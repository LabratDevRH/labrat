# The pons BUY flow for $LABRAT: research notes for a "buy rig"

Research only, for the owner's request "add pons buy buttons and all, just dont make real buys yet": for each buyback
batch the rat clicks through pons's own BUY flow on the real coin page, streamed live. Nothing was built into the
project. Nothing was signed or sent. Every run used a fresh throwaway in-memory key (`ponsbot.throwaway_account()`)
and the DRY balance override exactly as `live/brainrig.py` sets them up (`ponsbot.PonsBot(acct, 'DRY', size=(1280, 900),
theme='dark')`), and pons's one `eth_sendTransaction` per run was captured and answered with EIP-1193 4001.
`.env` was not opened. Chain reads were `eth_call` / `eth_estimateGas` only.

- Date: 2026-09-25, runs between unix 1790321069 and 1790321770 (latest block about 72,046,625).
- Page: `https://www.ponsfamily.com/launchpad/0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d`, viewport 1280x900, device scale 1.
- Probe scripts (in the session scratchpad, not the project): `buyrig_probe.py` (map / flow / masked / boxes / amount /
  reload), `buyrig_decode.py`, `buyrig_mask.js` (the mask is copied in full in the appendix below).
- Screenshots: `C:\Users\USER\AppData\Local\Temp\claude\C--Users-USER-claude\2c7fd74d-f439-4e77-a2f4-287f9e798649\scratchpad\buyrig_*.png`.
  `buyrig_01_*` to `buyrig_06_*` are UNMASKED research captures: they show a throwaway address, the launch wallet's short
  address and the claimable fees, so they are local only and must never be published. `buyrig_m_*` are masked, and
  `buyrig_m_*_frame.jpg` are the CDP screencast frames the stream would carry.
- About accepting pons's terms: every run except the first map accepted pons's Terms of Use / Privacy Policy gate for
  its throwaway wallet with `ponsbot.accept_terms`, the same routine the launch rig runs. That is 7 throwaway wallets.
  Acceptance is stored in the browser only (see step 1); no pons API call was made for it.

## TL;DR

1. **The flow**: [terms gate, once per browser context] → click the ETH amount field → type the amount → pons quotes
   ("Routed through the pons pool…") and enables **Buy LABRAT** → click it → pons's **Review buy** dialog (You send /
   You receive / Market "Uniswap v4 pool" / Max slippage 1%) → click **Confirm buy** → pons calls `eth_sendTransaction`
   (the button shows "Confirming") → DRY refuses 4001 → pons shows the toast "Trade did not settle" and the button goes
   back to "Buy LABRAT". There are no connect prompts: pons connects the injected wallet by itself. There are no slippage
   settings (the review shows a fixed 1%), and there is no signature request, no `wallet_switchEthereumChain` and no gas
   estimate through the wallet.
2. **What pons builds is byte-identical to `buyback.cd_router_buy(amountIn, minOut, deadline)`**: `to` = pons Universal
   Router `0x8876…0904`, `value` = amountIn, `execute(bytes,bytes[],uint256)` `0x3593564c`, commands `0x10` only
   (V4_SWAP, no SWEEP), actions `0x06 0x0c 0x0f`, the pinned LABRAT PoolKey, zeroForOne true, the 6-field swap struct
   with extra uint256 = 0 and empty hookData, **minOut = floor(quote × 99 / 100)** (quote = pons's v4 quoter, re-read at
   Confirm), and **deadline = now + 1200 s**. The request carries only `{from, to, value, data}`. `buyback.router_amounts`
   and `buyback.check_value` accept it as is. That also settles two open questions in BUYBACK_RESEARCH.md 1c: pons's UI
   builds `0x10` alone, not `0x10 0x04`, with the 6-field struct.
3. The exact captured transaction, run as `eth_call` with a 1 ETH state override for the sender right at capture, succeeds
   (returns `0x`); `eth_estimateGas` gives 160,968 to 161,163. Run 40 s later, it reverted
   `V4TooLittleReceived`: LABRAT moved more than 1% in that time. Simulate at capture time.
4. **Masking works with the CSS Custom Highlight API** (Chromium has it): an init script paints over every `0x…` string,
   the balance lines ("1 available", "0 available"), the creator-fee amounts, the wallet chip and pons's error-toast
   details. It changes no pons text and adds no nodes inside pons's tree, so React and the flow are untouched, hit-testing
   is unchanged, and the paint sits in pons's own layers (a modal still covers what is under it). Two CSS rules hide the
   Holders table and the toast description. Two full DRY runs × 9 checkpoints, including the profile menu and the Holders
   tab, found **0 unmasked** addresses or balances. The screencast frames were checked by eye.
5. Four points the build must handle:
   - the amount input is an invisible, **read-only hit box** until clicked, so brainrig's `MEASURE_JS` 'field' kind
     reports it "not visible"
   - the review dialog **animates in for about 0.4 s**
   - **Buy LABRAT jumps down 34 px** once the route line appears
   - pons's toast repeats the wallet's refusal message, so never send "DRY RUN" text. `ponsbot.wait_rejection` no
     longer sees the toast once its description is hidden: detect `.toast-title` "Trade did not settle" instead.

## 1. The flow, step by step (what a user does, and what the rat would click)

Every step below was run in DRY. Boxes are `[x, y, w, h]` in viewport CSS px at 1280x900.

**0. Page load (the rig, not the rat).** `page.goto(coin URL)`. `PonsBot.goto()` hard-codes the create page and
`PonsBot.settle()` waits for the create form's name field, so the buy rig needs its own goto/settle. Then:
`bot.dismiss_notice()`, `bot.set_theme()` (it was already dark), and the wallet shows as connected. The header chip reads
the wallet's short address (masked). The page is 2,448 px tall.

**1. Terms gate** (a wallet pons has not seen; the same modal as on the create page). It is a fixed dialog
`[role=dialog][aria-modal=true]` with the text "Review and accept / Required", at `[440, 191, 400, 517]`.
- Checkbox 1: `#legal-accept-terms`, input `[484, 527, 16, 16]`. Its label (brainrig `MEASURE_JS` 'terms' 0, which
  stops before the link) is `[512, 526, 167, 16]`, text "I have read and accept the".
- Checkbox 2: `#legal-accept-privacy`, input `[484, 584, 16, 16]`, label `[512, 583, 167, 16]`.
- Links to avoid: "Terms of Use" `[679, 526, 82, 16]` and "Privacy Policy" `[679, 583, 85, 16]` (each opens a new tab).
- **Accept and continue**: `[469, 640, 181, 44]`, disabled until both boxes are ticked. `MEASURE_JS` insets it to
  `[476, 647, 167, 30]` (corner inset 7).
- Also avoid: "Disconnect wallet" `[665, 641, 125, 41]`.
- While the gate is up, the amount input is read-only and covered by `div.dialog-backdrop`, and Buy is disabled.
- **Acceptance is stored in localStorage only:** the key `pons-legal-acceptance:2026-07-16-v1:<lower-case address>`. No
  pons API request is made for it. A reload in the same browser context with the same wallet does not show the gate
  again (tested). So in one browser context the gate appears once per session, not once per batch. A new terms version
  would show it again.

**2. The trade panel** (left column, `.convert-row` sides; a card of `[24, 693, 360, 601]` in page coordinates). It
opens in BUY mode:
- the top side is labelled "Sell": ETH, what you pay
- the bottom side is labelled "Buy": LABRAT, what you get

| element | selector | box at scrollY 0 (= page y) | box after the rig's reveal (scrollY 518) | the rat? |
|---|---|---|---|---|
| ETH amount field | `.convert-amount-field` containing `input[aria-label="Amount of ETH to spend"]` | `[68, 833, 272, 39]` | `[68, 315, 272, 39]` (43 high while editing) | **target** |
| pay asset | `button[aria-label="Pay with ETH"]` (listbox: ETH, WETH, USDG, cbBTC, stock tokens…) | `[68, 905, 100, 38]` | `[68, 387, 100, 38]` | never (keep ETH) |
| ETH balance line | `p.convert-balance` "1 available" + `button.convert-max` "Max" | `[228, 915, 112, 19]` | `[228, 397, 112, 19]` | never; **masked** |
| buy/sell toggle | `button[aria-label="Switch between buying and selling"]` | `[184, 942, 40, 40]` | `[184, 424, 40, 40]` | never |
| LABRAT side | `span.convert-currency` "LABRAT", `p.convert-balance` "0 available" | about y 1100 | about y 582 | never; balance **masked** |
| presets | `button[aria-pressed]` 25% / 50% / 75% / Max | y 1162, h 36: x 49 / 129 / 208 / 288, w 72 | y 644 | **never**: they are a share of the DRY 1 ETH override |
| route line | `p.token-buy-route` "Routed through the pons pool, 0.15% better" | appears with a quote | `[49, 699, 310, 15]` | no |
| **Buy LABRAT** | `button.ui-btn.ui-btn-primary`, text exactly "Buy LABRAT" | `[49, 1214, 310, 56]` (disabled) | `[49, 696, 310, 56]` → **`[49, 730, 310, 56]`** once the route line exists | **target** |

- The rig's reveal is `bot.reveal(amount, frac=0.35)`, which scrolls to scrollY 518. That view shows the amount field,
  both sides, the presets and Buy together (screenshot `buyrig_m_04_typed.png`).
- **The amount input is a hit box.** `input.convert-amount-input.is-hitbox` has opacity 0.01 and is `readonly` until
  clicked. The digits you see are a `span.rolling-number` strip of 0–9 cells under it.
  - A click at any point of the field (tested at 12 px from its left edge) focuses the input. The input becomes
    editable (`readonly` off, class `is-editing`, opacity 1) and the field grows from 39 to 43 px.
  - After a blur it is a read-only hit box again, and the value stays.
  - At the field's centre, `document.elementFromPoint` returns the input itself (`topIsInput`), so brainrig's
    `VERIFY_JS` works if the cue element is the field.
  - Brainrig's `MEASURE_JS` 'field' kind reports "not visible" (opacity 0.01 < 0.05) and read-only, so the buy rig
    needs an 'amount' kind: the box of `.convert-amount-field`, ready = visible, not covered, input on top at the
    centre.
  - After the rat's click, check `document.activeElement === input && !input.readOnly`, then do what brainrig does:
    `Control+A`, `Delete`, `keyboard.type(amount, delay=90)`, and read `input.value` back.
- After typing, pons re-quotes. About 3 s after the last key: a quote, the route line, and Buy enabled.
  - Observed states of the Buy button: "Buy LABRAT" disabled → "Buy LABRAT" enabled → (after Confirm) "Confirming"
    disabled with class `is-busy` → "Buy LABRAT" enabled after the 4001.
  - Clearing the field disables Buy again; the route line stays.
- The quote comes from two places:
  - pons's v4 quoter (`quoteExactInputSingle`, `0xaa9d21cb` on `0xe202…82Ef`), called through pons's RPC proxy
  - a comparison price: `POST /api/zeroex-swap {"sellToken":null,"buyToken":"0xaCa0…680d","sellAmountWei":"…","slippageBps":100}`
    → `{"price":{"buyAmount":"…","needsAllowance":false}}` (no wallet address in it)

  When the pool quote is higher, the route line says "Routed through the pons pool, X% better", and every run built the
  router pool buy. **Not observed:** what pons builds when 0x quotes better (probably a different `to`). The rig must
  refuse anything that `buyback.router_amounts` / `check_value` reject.

**3. Buy LABRAT → the Review buy dialog.**
- Selector: `[role=dialog][aria-modal=true]` whose `aria-labelledby` element reads **"Review buy"**. The review
  dialog's ids seen were `_R_edb_`; the terms dialog's were `_R_adb_`. React ids are not stable, so match on the text.
- Its lines, in order: "Review buy · You send · 0.0001 ETH · You receive · 849.2627 LABRAT · Market · Uniswap v4 pool ·
  Max slippage · 1% · Confirm buy · Cancel".
- **It animates in over about 0.4 s.** Dialog boxes sampled every 100 ms after the click: `[446, 231, 388, 470]` →
  `[444, 222, 392, 475]` → `[441, 211, 399, 483]` → `[440, 208, 400, 484]` → settled at **`[440, 208, 400, 485]`**.
  The rig must light Confirm only after its box has been stable (brainrig's `_acquire` 150 ms rule does this).
- **Confirm buy**: `[469, 623, 126, 44]`, enabled, on top at its centre.
- **Cancel**: `[611, 625, 54, 41]`. Clicking it closes the dialog and requests nothing from the wallet (tested).
- Use the dialog's lines as the pre-check: "You send" must equal the typed amount + " ETH", "Market" must be "Uniswap v4
  pool", and "Max slippage" must be "1%".

**4. Confirm buy → `eth_sendTransaction`.** Within about 0.5 s pons calls `eth_sendTransaction` once. Over the whole run
the wallet sees only `eth_accounts`, `eth_chainId` and that one send. There is no `wallet_switchEthereumChain` (the
launch flow has one), no `personal_sign` or typed data, and no `eth_estimateGas` through the wallet (pons reads through
its own `/api/robinhood-rpc`).

**5. After the 4001.** pons shows a toast at the top right, about `[896, 97, 359, 105]`:
`.toast-viewport[aria-label=Notifications] .toast-item[role=status]`, with the title `p.toast-title` **"Trade did not
settle"**, a description `p.toast-description`, and a close button `button.toast-close[aria-label=Dismiss]`.
- The description is viem's error text, `User rejected the request. Request Arguments: chain … from: <the page wallet,
  full address> to: … value: … data: <full calldata> Details: <the wallet's refusal message> Version: viem@2.56.0`.
- **So the refusal message must not say "DRY RUN".** The runs used `ponsbot.refusal('Simulated buy: not signed')`,
  which `ponsbot.refusal(message)` already allows, so ponsbot needs no change. The mask hides the description anyway.
- `ponsbot.wait_rejection()` reads `innerText`, and it returns None once the description is `visibility:hidden`. Detect
  the outcome from `.toast-title` (and, if needed, the description's `textContent`, which CSS does not affect).
- The amount stays in the field. The next batch is: the rat clicks the field again, and the rig selects all and types
  the new amount.

## 2. The transaction pons builds (decoded)

Three DRY captures: A and B were 0.0001 ETH (the test amount), C was 0.00001 ETH (one hit's worth).

```
request keys           {from, to, value, data}              (no gas, gasPrice, maxFee*, nonce, chainId, type)
from                   the page wallet (the throwaway)
to                     0x8876789976dEcBfCbBbe364623C63652db8C0904   pons Universal Router
value                  amountIn: 0x5af3107a4000 = 1e14 wei (A, B); 0x9184e72a000 = 1e13 (C)
data                   1,124 bytes, execute(bytes commands, bytes[] inputs, uint256 deadline)   0x3593564c
commands               0x10                                  V4_SWAP, one input
inputs[0]              abi.encode(bytes actions = 0x060c0f, bytes[3] params)
  params[0] 0x06       SWAP_EXACT_IN_SINGLE ((currency0 0x0000…0000 ETH, currency1 0xaCa0…680d LABRAT, fee 0,
                       tickSpacing 200, hooks 0xE5e7…e044), zeroForOne true, amountIn, amountOutMinimum,
                       uint256 0, hookData 0x)
  params[1] 0x0c       SETTLE_ALL (0x0000…0000 ETH, amountIn)
  params[2] 0x0f       TAKE_ALL (LABRAT, amountOutMinimum)   -> LABRAT goes to msg.sender (the page wallet)
poolId                 0xb0eb2633c73b39d2832643b62f54d99b71af752cf672df6b898a2210b8c2769d (the pinned one)
amountOutMinimum       floor(quote * 99 / 100), quote = quoter.quoteExactInputSingle(amountIn) re-read at Confirm
deadline               floor(unix time at Confirm) + 1200
```

| run | amountIn | quote (pons quoter) | amountOutMinimum | = floor(q×0.99)? | deadline − capture | sim at capture |
|---|---|---|---|---|---|---|
| A | 1e14 | 925.54 (the review showed 927.1565: pons re-quoted at Confirm; the quote was not logged in this run) | 916280887277970399589 | (consistent) | 1199 s | not run then; +40 s: reverts `V4TooLittleReceived(916.28…, 907.95…)` |
| B | 1e14 | 792759313319617037801 | 784831720186420867422 | **exact** | 1200 s | ok `0x`, gas 161,061 |
| C | 1e13 | 84100691543928061577 | 83259684628488780961 | **exact** | 1200 s | ok `0x`, gas 160,968 |

- The swap is the same shape for 0.0001 and 0.00001 ETH. pons shows no minimum amount.
- For every capture, `data == buyback.cd_router_buy(amountIn, minOut, deadline)` byte for byte,
  `buyback.router_amounts(data)` returns `(amountIn, amountIn, minOut, deadline)`, and
  `buyback.check_value(ROUTER, data, value)` is True.
- LABRAT is volatile (market cap moved between about $274k and $342k during the runs), so a 1% minimum can go stale
  within a minute. In DRY, simulate immediately at capture; a later `V4TooLittleReceived` only means the price moved.

**Suggested DRY checks** (the same spirit as `ponsbot.check_launch`, all from existing code):
- `to` == `buyback.ROUTER`
- selector `0x3593564c`
- `router_amounts` passes: commands `0x10` alone, actions `0x060c0f`, pinned pool key, zeroForOne, settle/take match
- `check_value`: value == amountIn == SETTLE_ALL
- amountIn == the typed batch amount in wei
- `from` == the page wallet
- extra uint256 == 0 and hookData empty
- canonical: equals `cd_router_buy(...)` byte for byte
- minOut > 0, and minOut ≥ 0.97 × a fresh quote at capture (a tolerance for price moves)
- now < deadline ≤ now + 1260
- no nonce / gas / chainId fields
- `eth_call` with the balance override succeeds

Then refuse with 4001 and a neutral message. LIVE is out of scope; note only that TAKE_ALL pays whichever wallet the
page is connected to.

## 3. Where addresses and balances appear, and how to mask them

Everything below was on the coin page for a connected wallet. "Short" means pons's `0x1234…abcd` form.

| where | DOM | what |
|---|---|---|
| header wallet chip | `button[aria-label="Profile"]` > span, `[900–905, 12, ~174, 40]` | page wallet, short |
| profile menu (opens from the chip) | `[role=menu]` (`#_r_0_`), first `[role=menuitem]` | page wallet, short + "Copy address" |
| About card | `p` "Creator 0x4C26…b893" `[49, 251, …]` | launch wallet, short |
| contract chip | `button[aria-label="Copy contract address"]` `[1008, 235, 125, 31]`; the Explorer link's href | token address, short |
| Creator fees card | `.token-creator-fees-amount` ×2 ("4.460506 ETH": earned / **claimable now**), "Claimable now, across every launch paid to 0x4C26…b893", "Payable to 0x4C26…b893. Connect that wallet to claim." | the launch wallet's claimable fees (a balance) + its address |
| Holder fee sharing card | "LABRAT pays its creator fees to 0x4C26…b893…" | launch wallet, short |
| trade panel | `p.convert-balance` "1 available" (ETH, **the DRY 1 ETH override**), "0 available" (LABRAT) | the page wallet's balances |
| Recent trades | `a[href^="/profile/0x"][title=<full address>]` "0x6505…40dc" per row (the row link's `aria-label` is "Buy 120.10K LABRAT") | traders, short |
| Holders tab | `#pons-v2-panel-holders` (tab `#pons-v2-tab-holders`) | holder addresses + **balances** + % |
| error toast | `.toast-description` | the page wallet in FULL, router, calldata |
| sticky header | translucent: page text scrolls under it, blurred but visible | whatever scrolls under it |

The following are not addresses or balances and stay visible: the description's "brain sha256 9778ea…" (no 0x), the
supply, price, market cap, trade sizes, the typed amount, the quote and the $ values. `title` / `href` attributes are
never painted (a headless screencast shows no tooltips or status bar).

**The approach (tested): an init script on the pons origin, main frame only, using the CSS Custom Highlight API**
(`CSS.highlights`, Chromium 105+; `hasHL` was true). The full script is in the appendix.
- A `MutationObserver` on the document (child list, subtree, character data) re-evaluates only the blocks that
  changed. A block is the nearest non-inline ancestor of a text node. The script joins the block's own text nodes with
  "\n", so a short address split over nodes ("0x6505", "…", "40dc") is still found, and turns each match into a DOM
  `Range` added to one `Highlight`.
- It paints with `::highlight(ratmask){color:transparent;background-color:#3a3a42}`: flat, neutral bars with no text in
  them, so no copy is added to the stream.
- Rules:
  - every `0x[hex]{3,}` string, including the short form, is masked
  - a block whose own text says "available" and has a digit is masked whole, which covers pons's rolling-digit strips
  - ETH amounts inside the "Creator fees" / "Holder fee sharing" cards are masked
  - three elements are masked whole: `button[aria-label="Profile"]`, `.token-creator-fees-amount`, `.toast-description`
  - two CSS rules in pons's own layers: `#pons-v2-panel-holders, .toast-description { visibility: hidden }`
- **Why highlights and not an overlay or text edits:**
  - Nothing inside pons's tree changes (no `nodeValue` edits, no wrapper spans), so React reconciliation and the flow are
    unaffected.
  - The paint belongs to the text, so it follows scrolling, the sticky header's blur and the dialog animations, and it
    respects z-order: a modal over a masked line hides the line and no bar is drawn across the modal. An absolute
    overlay would need z-order logic and could lag a frame.
  - Highlights take no pointer events, so brainrig's `elementFromPoint`-based `MEASURE_JS` / `VERIFY_JS` behave the
    same (tested: Confirm buy was on top at its centre, and the amount input was on top at its centre).
  - Mutation callbacks run as microtasks before the next paint, so new text is masked in the first frame that shows it.
- There is a fallback if the API is missing: `data-ratmask` on the block paints the whole block. It was not needed.
- Cost over a run of about 60 s: about 28 ms of main-thread time in about 100 mutation batches, 7–9 ms at most (the
  first parse batch).
- **An audit to run at every step: `window.__ratMaskAudit()`.** It is independent of the block logic. For every visible
  text node in the viewport that itself carries `0x…`, "available" or a fee amount, some mask range must intersect it,
  unless the node is `visibility:hidden`. It returns `{unmasked: [...], rects, stats}`.
  - The rig should withhold frames, and stop the run, whenever `unmasked` is not empty.
  - The rig should never stream a page whose origin is not pons: the script installs only on
    `https://www.ponsfamily.com`, and a `target=_blank` link opens outside the streamed tab.

**Verification done (DRY, headless Chromium, CDP screencast JPEG q70 like brainrig):**
- Two full flows (0.0001 and 0.00001 ETH), each with 9 checkpoints:
  1. load with the terms modal
  2. top of the page after accepting
  3. the panel in view
  4. the amount typed
  5. the review dialog
  6. the toast after the 4001
  7. the profile menu open
  8. the Holders tab
  9. the Recent trades tab
- `__ratMaskAudit().unmasked` was empty at all 18 checkpoints.
- In lossless screenshots taken at the same moments, every masked text rect is one flat colour (max channel spread
  ≤ 6), except rects that are not leaks:
  - the SVG icons inside the whole-masked wallet chip (user icon, chevron): icons are not text
  - the fee amounts under the terms modal: the modal covers them
  - the hidden toast description
- The screencast frames (`buyrig_m_*_frame.jpg`) were checked by eye: header chip, creator / contract / trader
  addresses, the fee amounts, "1 available" and "0 available" are bars; the toast shows only "Trade did not settle";
  the Holders table is blank.
- About 1,000 screencast frames per run.
- The first mask version had two bugs that the checks caught, both fixed in the appendix version:
  - its regex ran over text joined across elements, so it masked "Bac" of "Back to explore" and the "E" of "Explorer"
  - `\b` failed on "1 availableMax", which left the ETH balance unmasked

**Limits:**
- The rules are pattern- and class-based. A new pons layout could add, for example, a "Balance: 1.0 ETH" line or a new
  card that the rules do not catch. The audit catches only the patterns it knows. So keep the rig's view on the trade
  panel, keep the audit gate, and re-check the frames by eye after any pons redesign.
- Masking hides text only: the wallet chip's avatar icon stays visible (it is generic).

## 4. What a buy rig needs (design notes; nothing here is built)

- **Reuse without modifying brainrig.py / ponsbot.py:**
  - `PonsBot(acct, 'DRY', size=(1280, 900), theme='dark', symbol='LABRAT')` + `start()` (provider, DRY override,
    bindings), then `page.add_init_script(mask)` + brainrig's `OVERLAY_JS` (the cursor and cue sit above the highlights).
    Then `page.goto(coin URL)`, `dismiss_notice()`, `set_theme()`, and your own "connected" wait.
  - `accept_terms` / `MEASURE_JS` 'terms' + 'accept' for the gate.
  - `ponsbot.refusal(message)` with a message without "DRY RUN".
  - `buyback.router_amounts` / `check_value` / `cd_router_buy` / `cd_quote` for the checks.
  - brainrig's `session.target` / `hold`, `_acquire` stability, `VERIFY_JS`, `FOCUS_JS`, the screencast pump and
    `_mouse_loop`, all adapted in a new file.
- **Targets per batch:**
  1. the amount field (new 'amount' kind; consequence: type the batch amount, read it back, wait for Buy to be enabled
     and the route line)
  2. **Buy LABRAT** (re-measure after the 34 px jump; consequence: wait for the "Review buy" dialog and check its
     lines)
  3. **Confirm buy** (light it only after the dialog is stable; consequence: capture, decode, check, simulate, refuse
     4001, wait for `.toast-title`)

  The first batch of a session adds pons's terms targets (2 labels + Accept and continue).
- **Never lit:** the presets, Max, the pay-asset selector, the buy/sell toggle, the profile chip, the Holders tab,
  Disconnect wallet, and the terms/privacy links. Clicks outside the lit target are never forwarded (the brainrig rule),
  so the rat cannot open them.
- **DRY balance:** pons reads the wallet's ETH with multicall3 `aggregate3` (`0x82ad56cb` on `0xcA11…CA11`) through its
  own `/api/robinhood-rpc`. `PonsBot._route_rpc` adds the 1 ETH state override, so the panel shows "1 available" and
  enables Buy. That line is masked. It is not verified what pons does with a real zero balance (it probably disables
  Buy).
- **Website copy:** pons's review and toast are pons's own UI. Anything the site adds for these buys must say
  "Simulated": the batch amount, pons's decoded minimum out, the simulation result. The site must show no address, no
  balance and no "dry run" / "test" wording.

## Sure vs. not sure

**Sure (observed in these runs):**
- the selectors and boxes above at 1280x900
- the terms gate and its localStorage key
- the hit-box behaviour of the amount input
- the review dialog's text and its animation
- the button states
- the exact calldata shape and its byte identity with `cd_router_buy`
- minOut = floor(quote × 0.99) (runs B and C; run A is consistent with a re-quote at Confirm, but its quote was not
  logged)
- deadline = now + 1200 s
- the wallet calls pons makes
- the toast's structure and text
- that the mask covers every address and balance place listed, on every checkpoint

**Not sure / not established:**
- what pons builds when the 0x price beats its pool (never happened in these runs)
- what pons shows with a real zero balance
- whether the terms version (`2026-07-16-v1`) or the layout will change
- whether pons ever shows another balance or address place not seen here: the profile page, other tabs, a sell flow
  and a real completed trade were not examined

## Appendix: the mask script (`buyrig_mask.js`, as tested)

```js
(() => {
  // pons stream mask (research draft for the buy rig). Paints over every 0x string, the wallet's balance lines
  // ("N available"), the creator-fee amounts and pons's error-toast details, with the CSS Custom Highlight API:
  // pons's DOM is not edited (no text changed, no node added inside pons's tree), so React and the buy flow are
  // untouched, and the paint lives in pons's own layers (a modal above a masked line still covers it; a masked line
  // inside a modal or toast is masked). Hit-testing is unaffected (highlights take no pointer events).
  if (window.top !== window) return;
  if (location.origin !== "https://www.ponsfamily.com") return;
  if (window.__ratMask) return;
  const SEP = "\n";
  // full or shortened 0x strings; a shortened one may be split over text nodes ("0x6505", "\u2026", "40dc")
  const HEX = /0x[0-9a-fA-F]{3,}(?:\n?(?:\u2026|\.{2,3})\n?[0-9a-fA-F]{2,})?/g;
  const HEX1 = /0x[0-9a-fA-F]{3}/;
  const ETH = /\d[\d.,]*\s*[kKmMbB]?\s*ETH/g;               // amounts: only inside the creator-fee / holder-fee cards
  const FEE_CARD = /^\s*(creator fees|holder fee sharing)/i;
  // elements masked whole (every text inside): the wallet chip, the fee amounts, pons's toast details
  const WHOLE = 'button[aria-label="Profile"],.token-creator-fees-amount,.toast-description';
  const INLINE = /^(SPAN|A|B|STRONG|EM|I|SMALL|CODE|ABBR|TIME|BDI|BDO|LABEL|SUP|SUB|MARK|S|U)$/;
  const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE|TEXTAREA|INPUT|SELECT|OPTION)$/;
  const hasHL = !!(window.CSS && CSS.highlights && window.Highlight);
  const hl = hasHL ? new Highlight() : null;
  if (hl) { hl.priority = 1000; CSS.highlights.set("ratmask", hl); }
  const byEl = new Map();          // element -> its mask ranges
  const stats = {updates: 0};
  window.__ratMask = {hasHL, byEl, stats};

  function style() {
    if (document.getElementById("__ratmaskstyle") || !document.documentElement) return;
    const s = document.createElement("style");
    s.id = "__ratmaskstyle";
    s.textContent =
      "::highlight(ratmask){color:transparent;background-color:#3a3a42;text-decoration:none;text-shadow:none}" +
      // belt and braces, in pons's own layers: the holders table (balances) and the toast details never show
      "#pons-v2-panel-holders,.toast-description{visibility:hidden!important}" +
      // fallback when the Highlight API is missing: the whole element is painted over
      "[data-ratmask]{color:transparent!important;background:#3a3a42!important;border-radius:4px}" +
      "[data-ratmask] *{visibility:hidden!important}";
    document.documentElement.appendChild(s);
  }

  function blockOf(n) {
    let e = n && n.nodeType === 3 ? n.parentElement : n;
    for (let i = 0; e && i < 4 && INLINE.test(e.tagName) && e.parentElement && e.parentElement !== document.body; i++)
      e = e.parentElement;
    return e;
  }

  function target(n) {
    const p = n && n.nodeType === 3 ? n.parentElement : n;
    const w = p && p.closest ? p.closest(WHOLE) : null;
    return w || blockOf(n);
  }

  function okText(t) {
    const p = t.parentElement;
    return !!p && !SKIP.test(p.tagName) && !p.closest("#__ratcur,#__ratcue,#__ratmaskstyle");
  }

  // the block's OWN text nodes (not those of nested blocks), joined with SEP
  function ownTexts(e) {
    const out = [];
    const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let t, pos = 0;
    while ((t = w.nextNode())) {
      if (!okText(t) || blockOf(t) !== e || t.parentElement.closest(WHOLE)) continue;
      const v = t.nodeValue || "";
      out.push({t, a: pos, b: pos + v.length});
      pos += v.length + SEP.length;
    }
    return out;
  }

  function point(list, off) {
    for (const x of list) if (off <= x.b) return {t: x.t, o: Math.max(0, Math.min(off - x.a, x.b - x.a))};
    const l = list[list.length - 1];
    return {t: l.t, o: l.b - l.a};
  }

  function feeCard(e) {
    for (let d = e, i = 0; d && d !== document.body && i < 7; d = d.parentElement, i++) {
      const s = d.textContent || "";
      if (s.length < 1500 && FEE_CARD.test(s)) return true;
    }
    return false;
  }

  function rangesFor(e) {
    if (!e || !e.isConnected || !e.matches) return [];
    if (e.matches(WHOLE)) { const r = new Range(); r.selectNodeContents(e); return [r]; }
    const list = ownTexts(e);
    if (!list.length) return [];
    const s = list.map(x => x.t.nodeValue || "").join(SEP);
    const spans = [];
    // a balance line ("1 available", its digits may be a rolling-digit strip): the whole line
    if (/available/i.test(s) && /\d/.test(s)) spans.push([0, s.length]);
    else {
      HEX.lastIndex = 0; let m;
      while ((m = HEX.exec(s))) spans.push([m.index, m.index + m[0].length]);
      if (/\dETH|\d\s*ETH/.test(s) && feeCard(e)) { ETH.lastIndex = 0; while ((m = ETH.exec(s))) spans.push([m.index, m.index + m[0].length]); }
    }
    return spans.map(([a, b]) => {
      const r = new Range();
      const p = point(list, a), q = point(list, b);
      r.setStart(p.t, p.o);
      r.setEnd(q.t, q.o);
      return r;
    });
  }

  function update(e) {
    if (!e) return;
    stats.updates++;
    const old = byEl.get(e);
    if (old) {
      if (hl) old.forEach(r => hl.delete(r)); else e.removeAttribute("data-ratmask");
      byEl.delete(e);
    }
    const rs = rangesFor(e);
    if (!rs.length) return;
    byEl.set(e, rs);
    if (hl) rs.forEach(r => hl.add(r)); else e.setAttribute("data-ratmask", "1");
  }

  function scan(root) {
    if (!root) return;
    if (root.nodeType === 3) return update(target(root));
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    const seen = new Set();
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) {
      if (!okText(t)) continue;
      const e = target(t);
      if (e && !seen.has(e)) { seen.add(e); update(e); }
    }
  }

  function prune() {
    for (const [e, rs] of byEl) if (!e.isConnected) { if (hl) rs.forEach(r => hl.delete(r)); byEl.delete(e); }
  }

  const mo = new MutationObserver(recs => {
    const t0 = performance.now();
    stats.batches = (stats.batches || 0) + 1;
    const done = () => { const dt = performance.now() - t0; stats.ms = (stats.ms || 0) + dt; stats.maxMs = Math.max(stats.maxMs || 0, dt); };
    const touched = new Set();
    for (const r of recs) {
      if (r.type === "characterData") touched.add(target(r.target));
      else { touched.add(target(r.target)); r.addedNodes.forEach(scan); }
    }
    touched.forEach(e => e && update(e));
    prune();
    style();
    done();
  });
  mo.observe(document, {childList: true, subtree: true, characterData: true});
  style();
  if (document.body) scan(document.body);

  // audit, independent of the block logic: every visible text node that itself carries a 0x string, a balance line
  // or a fee amount must intersect a mask range (or sit in a hidden element). -> {unmasked, masked, rects}
  window.__ratMaskAudit = () => {
    const unmasked = [], rects = [], all = [];
    for (const rs of byEl.values()) for (const r of rs) {
      all.push(r);
      for (const q of r.getClientRects()) if (q.width > 0 && q.height > 0 && q.bottom > 0 && q.top < innerHeight)
        rects.push([Math.round(q.x), Math.round(q.y), Math.round(q.width), Math.round(q.height)]);
    }
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode())) {
      if (!okText(t)) continue;
      const v = t.nodeValue || "", p = t.parentElement;
      const sens = HEX1.test(v) || /available/i.test(v) || !!p.closest(".token-creator-fees-amount")
        || (/\d\s*ETH/.test(v) && feeCard(p));
      if (!sens) continue;
      const rr = document.createRange(); rr.selectNodeContents(t);
      const vis = [...rr.getClientRects()].some(q => q.width > 0 && q.bottom > 0 && q.top < innerHeight)
        && getComputedStyle(p).visibility !== "hidden";
      if (!vis) continue;
      if (!all.some(r => r.intersectsNode(t))) unmasked.push({text: v.trim().replace(/0x[0-9a-fA-F]{4,}/g, "0x<hex>").slice(0, 80), tag: p.tagName, cls: String(p.className).slice(0, 60)});
    }
    return {hasHL, unmasked, masked: byEl.size, rects, stats};
  };
})();
```
