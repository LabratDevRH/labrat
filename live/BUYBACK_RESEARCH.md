# $LABRAT buyback: research notes

Research only, for option 1 ("every correct press buys $LABRAT"). Nothing here was built, signed or sent. Every
chain read below was an `eth_call`, `eth_estimateGas` or `eth_simulateV1` (a read-only simulation that also
returns logs) against `https://rpc.mainnet.chain.robinhood.com` (chain id 4663). `.env` was not opened. Unless a
line says otherwise, the numbers are pinned to **block 71706358** (timestamp 1790286748, 2026-09-25).

Framing for anything built on this: the rat's brain is two trained artificial networks, not a biological brain, and
it does not understand money. The code sets the rules. A hit in training only triggers a rule that the code
defines.

## TL;DR

1. **$LABRAT has already left its bonding curve.** It filled the 4.2 ETH curve and graduated at block 71688341,
   about 4,238 blocks (about 7 minutes) after the launch. `buy()` on the curve now reverts with
   `CurveGraduated()`. A buyback today has to buy from the coin's **Uniswap v4 pool** through the pons Universal
   Router, not from the curve.
2. **Pool buy:** `execute(bytes,bytes[],uint256)` (selector `0x3593564c`) on `0x8876789976dEcBfCbBbe364623C63652db8C0904`,
   `msg.value` = ETH in, commands `0x10` (V4_SWAP), actions `0x06 0x0c 0x0f` (swap exact-in single, settle all, take
   all). A simulated 0.00001 ETH buy from the launch wallet delivers **349.132310919230143372 LABRAT** to the launch
   wallet, exactly what the pons v4 quoter predicts. Minimum-out and the deadline are enforced (both tested).
3. **Curve buy** (for any coin still on its curve): `buy(uint256 quoteIn, uint256 minTokensOut, address recipient)`
   payable, selector `0x59a87bc1`, `msg.value` must equal `quoteIn` exactly. It was matched against 9 real buys on
   the LABRAT curve and simulated on a live curve: the returned tokens equal the constant-product quote to the wei.
4. **Creator fees** are credited in **ETH** to the launch wallet inside pons's **FeeEscrow**
   `0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e`. They are read with `balanceOf(address)` (`0x70a08231`) and claimed
   with `claim()` (`0x4e71d92d`) or `claim(uint256)` (`0x379607f5`), sent from the launch wallet. **Claimable at block
   71706358: 1,166,469,550,052,640,996 wei = 1.16647 ETH.** Nothing has been claimed: the wallet's nonce is 1, which
   is the launch tx. A pons operator moves new fees into the escrow about every 240 blocks (about 24 s).
5. Cost: a router buy estimates at **163,889 gas**. At the 0.04365 gwei gas price that is about 0.0000072 ETH, which is
   about 72% of a 0.00001 ETH buy. Each pool buy also pays a 3% hook cut, taken from the LABRAT it delivers: 1% to
   pons and 2% creator tax, which comes back to the launch wallet as ETH.

## How this was established (and what could not be)

- **Blockscout API:** every `robinhoodchain.blockscout.com/api/...` request returned HTTP 403 with a Cloudflare
  "managed challenge" page. That is bot detection, so I did not try to get around it. **I have no verified source
  and no verified ABI from blockscout** for any contract here. I do not know whether they are verified there.
- Instead, all of the following:
  1. The **dispatch-table selectors** of each contract, read from its deployed bytecode (`eth_getCode`: every
     `PUSH4 x; EQ`).
  2. **Names** for those selectors and for the event topics from the public openchain signature database. Each name
     is a keccak preimage, and I recomputed every hash locally, so the *types* are certain. Parameter *names* are my
     reading of the values (for example "quoteIn"). They are not from source.
  3. **Real transactions by other users**, decoded (input, value, receipt logs): 9 direct curve buys and 2 curve
     sells on the LABRAT curve, 4 pons-router pool buys of LABRAT, and real FeeEscrow claims by other creators.
  4. **Simulations** from the launch wallet (eth_call / eth_simulateV1), including the failure cases (wrong value,
     minimum-out plus 1, a past deadline, over-claiming, a stranger claiming). Each one reverted with the error you
     would expect.
- Historical state: the Robinhood RPC only serves recent state (a block about 10,000 back already failed), and the
  publicnode RPC wants a token for archive reads. So I could not simulate a curve buy on LABRAT *before*
  graduation. The curve ABI is proven on a curve that is still live (below) and on LABRAT's own 9 real curve buys.

## The contracts

| what | address | how we know |
|---|---|---|
| LABRAT token | `0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d` | launch receipt, TokenLaunched topic 1 |
| LABRAT bonding curve | `0x174E4Cc2A44811Ed85eB2589C119B723a71AE024` | launch receipt: TokenLaunched topic 2, the 1B-token mint goes to it, `Initialized(token)` and `SnipeTaxExempted(launch wallet)` come from it; `token.curve()` and factory `getLaunchedToken(token)[1]` return it |
| pons v2 factory | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` | launch tx `to`; curve `factory()` |
| FeeEscrow (creator fees) | `0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e` | curve, factory and hook `feeEscrow()`; 75 `Credited(launch wallet, ...)` events |
| pons v4 hook ("MemeHook", fee policy) | `0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044` | factory `memeHook()`, curve `feePolicy()`, `hooks` field of every LABRAT pool swap |
| Uniswap v4 PoolManager | `0x8366a39CC670B4001A1121B8F6A443A643e40951` | factory, router and quoter `poolManager()`; LABRAT leaves it on every pool buy |
| pons Universal Router | `0x8876789976dEcBfCbBbe364623C63652db8C0904` | the target of real LABRAT pool buys; `execute` selectors in its bytecode |
| pons v4 quoter | `0xe202BB8dd524eE9C5E679e5B5809f7A373a982Ef` | `quoteExactInputSingle` in its bytecode; `poolManager()` is the one above; its quote equals the simulated buy to the wei |
| fee sweep operator (pons) | `0x49bBf2b70955fb3A106e084d4bFDa92d334573d2` | hook `feeSweepOperator()`; it sends every `sweepFees` / `sweepPoolFees` tx |
| protocol fee recipient / factory owner | `0x263ED295daFaE1d9aaDd6E56c4B6f9F38EE019DD` | Safe (`SafeReceived` events); curve `protocolFeeRecipient()`, factory `owner()` |
| buyback vault (pons native buyback) | `0x42dF2a798f82289E177311362E8f5cCc45c1219C` | curve / factory / hook `buybackVault()` |

The factory record for LABRAT, `getLaunchedToken(address)` (`0x3cf28b5a`), returns
`(token, curve, deployer = launch wallet, creatorFeeRecipient = launch wallet, pairToken = 0x0 (ETH),
graduationThreshold = 4.2e18, poolFee = 0, tickSpacing = 200, creatorTaxBps = 200, buybackEnabled = false,
phase = 2, 0, 0, 0, exists = true)`.

## 1. Buying

### 1a. On a bonding curve: `buy(uint256,uint256,address)`, selector `0x59a87bc1`, payable

```
function buy(uint256 quoteIn, uint256 minTokensOut, address recipient) payable returns (uint256 tokensOut)
event CurveBuy(address indexed buyer, address indexed recipient, uint256 quoteIn, uint256 tokensOut,
               uint256 fee, uint256 creatorTax)                  // topic0 0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455
error NativeValueMismatch(uint256 value, uint256 quoteIn)        // 0xbc760cfe
error SlippageExceeded(uint256 minOut, uint256 out)              // 0x71c4efed
error CurveGraduated()                                           // 0x025ac17e
```

- **Pays:** native ETH. `msg.value` must equal `quoteIn` exactly. Value −1, value +1 and value 0 each revert with
  `NativeValueMismatch(value, quoteIn)`. Every real direct buy had `value == arg0`.
- **Slippage:** `minTokensOut`. With exactly the quoted amount the call succeeds; with quoted + 1 it reverts with
  `SlippageExceeded(quoted+1, quoted)`. Real buys used either a real minimum or `1`.
- **Recipient:** the third argument. In a simulation with recipient `0x2222…`, the token `Transfer` went to
  `0x2222…`, and `CurveBuy` had topic1 = sender and topic2 = recipient.
- **Deadline:** there is none in `buy`.
- **Return value:** tokens out (uint256).
- **Fees** come off the input: `feeBps` (100 = 1%) plus the coin's `creatorTaxBps`, plus a snipe tax in the first
  `snipeTaxSeconds()` = 3 s (`SnipeTaxCharged(address,uint256)`, from 9900 bps down to 0). The launch wallet is
  `snipeTaxExempt` on LABRAT.
- **Tokens out** = `a*tokenReserve / (10000*quoteReserve + a)`, where `a = (quoteIn - fee - tax) * 10000` and the
  reserves come from `getReserves()` (`0x0902f1ac`). This matched the simulated return exactly.
- Evidence from real LABRAT curve buys (all selector `0x59a87bc1`, value == arg0, CurveBuy matches):
  `0x6bc7145b597af26329854d2182c83e5f8339e3e17d40626fa18f96a9e7e41373`,
  `0x10352fb35ffd05ae8b1d0dc28a5ea63f70125cae6ecbb06d3d94cbb1d1c30bd8`,
  `0x93e0b473b5cb687fb7700190317ec967967b8736bddd1b3c92279ecb50ca2ff7`,
  `0x9db2b809b8cb9c6336de8e02a3ff5b211b419d4bd672f61a2dd2bec3bd014fa8`,
  `0x9ff0962c20ddf8bf9342738d329353cdfcdb2b520031e818804ac037cc488c27`,
  `0x10a493700ee7e82817f3655742b311713bf956b46464b03429cfbf49a0d4a393`,
  `0x08088d657ebffd2217478b47ec4daf1740f229944655fc27319cf2aea47ddc41`,
  `0x17bcb28cb3e30cb0882a3ada3e723b2c40fad18077d16d90b99e0d8d2e24a8ee`,
  `0x456db670fa7af1d13d99d2e7ae5815cbb6fc7da03960941d27b0733cfdbe7e13` (7 different senders).
- Curve sell for reference: `sell(uint256 tokensIn, uint256 minQuoteOut, address recipient)`, selector `0xd04c6983`,
  event `CurveSell(address,address,uint256,uint256,uint256,uint256)`
  (`0x5b0c5e3a4c235a6216025a51c65766ffae26cf209f218ca7be9deb78d224d5bb`, `0xdbe6a6d3d56c689e9bf3b0c1f2e73633252921e6e70d11a4acc5a275073cb9df`).

### 1b. Graduation (what happened to LABRAT, and how the engine can tell)

- The buy that reaches 4.2 ETH graduates the coin **in the same transaction**. For LABRAT that was
  `0x6a2a6092177c8f6d6526ed9f0cdbc65234ba491bb62c256f14df240b3e0e78ba` (block 71688341, a bot contract buying
  0.15 ETH). Its logs, in order: `CurveBuyRefunded(buyer, 3470322288145336)` (the part of the ETH it could not use
  was returned), `CurveBuy`, the fee sweep, `CurveCompleted(address factory, uint256 4200000000000000049, uint256
  285714285714285714285714285)`, factory events, PoolManager `Initialize`, a position minted and locked (locker
  `0x267444d0…`), and hook `PoolRegistered(bytes32,address,address,address)`.
- Afterwards: `graduated()` (`0xe7c2b772`) = true, `sellableTokens()` = 0, `realQuoteReserve()` = 0, and the factory
  record's phase = **2**. A curve that is still live (checked on `0x2E6A4c9E…`) has phase **0**. Phase values 1 and 3
  exist in the enum, but I did not observe them. The names used for them in `~/claude/flycoinrh/pons.py` ("settling",
  "closed") are unverified here.
- `buy()` on the LABRAT curve now reverts `CurveGraduated()` (`0x025ac17e`): proven below.
- Detection for an engine: read `graduated()` on the curve, or `getLaunchedToken(token)` field 10, before every buy.
  Phase 0 means use the curve, phase 2 means use the pool, and anything else means stop. The `CurveCompleted` topic
  is `0xf8d37a90738ae063b8b8058b66f5880cf3cf7ab0c5d4fa78219696591dfbfb67`.

### 1c. In the v4 pool (what LABRAT needs now): pons Universal Router `execute(bytes,bytes[],uint256)`, `0x3593564c`

```
router.execute(commands = 0x10,                      // V4_SWAP
               inputs   = [ abi.encode(bytes actions = 0x060c0f, bytes[] params) ],
               deadline)                            payable, msg.value = amountIn (native ETH)
params[0]  SWAP_EXACT_IN_SINGLE (0x06):
           abi.encode( ((address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks),
                        bool zeroForOne, uint128 amountIn, uint128 amountOutMinimum, uint256 <extra, 0>, bytes hookData) )
params[1]  SETTLE_ALL (0x0c): abi.encode(address currency = 0x0 (ETH), uint256 maxAmount = amountIn)
params[2]  TAKE_ALL   (0x0f): abi.encode(address currency = LABRAT, uint256 minAmount = amountOutMinimum)
LABRAT PoolKey = (0x0000000000000000000000000000000000000000, 0xaCa07FE3BC5fF3e7501cA1dCCFCd937fD710680d, 0, 200,
                  0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044), zeroForOne = true (ETH -> LABRAT)
poolId = keccak(abi.encode(PoolKey)) = 0xb0eb2633c73b39d2832643b62f54d99b71af752cf672df6b898a2210b8c2769d
errors: V4TooLittleReceived(uint256 min, uint256 got) 0x8b063d73, TransactionDeadlinePassed() 0x5bf6f916
```

- **Pays:** native ETH as `msg.value` (= amountIn = SETTLE_ALL max). **The router does not refund excess ETH:**
  `execute` with `msg.value` = 2 × amountIn succeeds (re-checked with `eth_call`), and the surplus stays in the router.
  `live/buyback.py` therefore checks that the transaction's value equals the swap's amountIn and the SETTLE_ALL amount
  before it simulates or signs (`check_value`).
- **Slippage:** `amountOutMinimum`, enforced again by TAKE_ALL's `minAmount`. Quoted amount: the call succeeds.
  Quoted + 1: it reverts `V4TooLittleReceived(quoted+1, quoted)`.
- **Recipient:** there is no parameter for it. TAKE_ALL pays the caller (`msg.sender`). The simulation shows
  `Transfer(PoolManager -> launch wallet, 349132310919230143372)`.
- **Deadline:** the third argument of `execute`. A past deadline reverts `TransactionDeadlinePassed()`.
- **Hook cut:** on a buy the hook takes 3% of the gross LABRAT output. In the simulation it was 10.797906523275159072
  to the hook and 349.132310919230143372 to us, which is exactly 3.0000%. That 3% is `hookFeeBps` 100 plus the 200
  bps creator tax. The pending balances in the hook confirm the 1 : 2 split.
- **Quote:** quoter `quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))`, selector
  `0xaa9d21cb`, returns `(amountOut, gasEstimate)`. The hook's cut is already taken out of that amountOut.
- **The extra `uint256`** in the swap struct (word 9): 3 of the 4 real pons-router LABRAT buys I decoded used this
  6-field layout with the value 0:
  `0x09314d65ad88e84903b15c48e6356e1f90dd8ed13eefa731a68e66cd08e43e0c`,
  `0x1911d31d75da9898f9d5d5de1b197efad7143efc29306d5678a16ca73e017ea0`,
  `0x1d11419fa8ee78cd5b8adced7536c80e04306c0beed3225d434e23f5b9d06ac2`,
  `0x24cd08a51183dae40ed186d24eb2f5c24187233ecbaaa591201e140c2462fd4f` (4 different senders). One
  (`0x1bf45139181c380860d92e71ff99aed45292b65578583e3cd5072719e2c3431e`) used the classic 5-field layout and also
  succeeded. I cannot see the source, so I can't say why both work. The 6-field layout is the one that simulated
  correctly here. Its field name ("minHopPriceX36" in the flycoinrh notes) is **not verified**.
- The real router buys came in two shapes: commands `0x10` alone, and `0x10 0x04` (V4_SWAP + SWEEP(ETH, recipient
  `0x…01` = the caller, 0)). I did not confirm which one pons's own UI builds. The simulation used `0x10` alone.
- One more real buy went to a different router (`0x204faca1764b154221e35c0d20abb3c525710498`, actions `0x070b0e`). I
  did not examine it.

## 2. Creator fees: where they sit and how the launch wallet claims them

### Flow (all observed on chain)

1. **On the curve** (until block 71688341): each buy or sell adds `fee` and `creatorTax` (quote asset, ETH) to the
   curve's balances. The pons operator `0x49bBf2b7…` calls `sweepFees(uint256)` (`0x3729bb9a`) on the curve, which
   emits `FeesSwept(uint256 protocol, uint256 buyback, uint256 creator)` (topic `0x9f4cd7c4…`) and pays the creator
   part into the FeeEscrow. In all 19 LABRAT sweeps, the third word equals the `Credited` amount for the launch
   wallet in the same tx.
2. **In the pool** (since graduation): the hook keeps per-pool, per-currency pending balances.
   `pendingCreatorTax(bytes32 poolId, address currency)` is `0xc8eaa792`. Buys leave the tax in LABRAT and sells
   leave it in ETH. The operator calls hook `sweepPoolFees(bytes32,uint256,uint256)` (`0x3d61055e`). In the tx I
   inspected (`0x174bdf8edb7d26b136dc2782bb4469733927239d0f8017d8ba3e74d9cc88e8b9`), the hook swaps its LABRAT
   through the PoolManager (a `Swap` event) and then credits the ETH to the FeeEscrow for the launch wallet. The
   hook's `maxInternalPriceImpactBps()` = 300 limits that internal swap.
3. **FeeEscrow** `0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e` holds the **ETH** per account. Since the launch it has
   emitted 75 `Credited(address indexed account, address indexed from, uint256 amount)` events
   (`0x4e45da441832cf53bdaa69235704fc0575e68210f459ee1562911024b12967d5`) for the launch wallet: 20 from the curve
   and 55 from the hook. The median gap is 238.5 blocks, about 24 s. There have been no `CreditedToken` events for
   the launch wallet, so so far everything has been credited as ETH.

### FeeEscrow ABI (selectors from its bytecode, types from the hashes)

```
balanceOf(address account) view returns (uint256)          0x70a08231   // claimable ETH
claim()                                                      0x4e71d92d   // all of msg.sender's balance
claim(uint256 amount)                                        0x379607f5   // part of it
balanceOfToken(address,address) view                         0xf59e38b7   // token credits (0 for us)
claimToken(address) / claimToken(address,uint256)            0x32f289cf / 0x1698755f
credit(address) payable / creditToken(address,address,uint256)   0xd5d44d80 / 0x09ad4dd9   // used by pons contracts
event Credited(address indexed account, address indexed from, uint256 amount)
event Claimed(address indexed account, uint256 amount)              0xd8138f8a3f377c5259ca548e70e4c2de94f129f5a11036a15b69513cba2b426a
event CreditedToken(address,address,address,uint256) / ClaimedToken(address,address,uint256)
error InsufficientBalance(uint256 requested, uint256 balance)   0xcf479181
error NoBalance()                                               0xc2caa2a6
```

- **Claim semantics come from real claims by other creators.** Example: `claim(uint256)` in
  `0x104ee88bb47e2cbcdf476c0cd279061e5ff029b0255cac2b8d4d625a47f5da7f` took escrow `balanceOf` from 1009799999999999
  to 499799999999999. The claimer's ETH rose by exactly 510000000000000 plus the gas it paid, and the escrow's ETH
  fell by the same amount. Two more claims
  (`0xc84c6515a8c3018783932f35ae35af80e7e21bbdc86669bcf7654bd04d38cba8`,
  `0x097198b852be6070c5b3d68749297ee79a2f4c1369362287b9eafc312e584bdf`) show the same thing.
  Recent `claim()` calls (`0x4e71d92d`) were sent straight from the claiming account.
- A claim has to be sent **by the launch wallet itself**. From an address with no credit (re-checked with
  `eth_call` on 2026-09-25), `claim()` and `claim(0)` revert `NoBalance()`, while `claim(uint256 x)` with x > 0 reverts
  `InsufficientBalance(x, 0)`. From the launch wallet, asking for more than the balance reverts
  `InsufficientBalance(requested, balance)`. The engine uses `claim(uint256)`, so a wrong sender shows up as
  `InsufficientBalance`, not `NoBalance`.
- The protocol and creator split on the curve is `protocolFeeShareBps()` = 3000 of `feeBps`. This comes from the
  getters. I did not fully reconcile the split against FeesSwept amounts, because the snipe tax complicates the early
  ones.

## 3. The proofs (block 71706358, from the launch wallet `0x4C2661717B97cd23aa87Fe29fE0C50CFf2CBb893`)

The launch wallet held 4,319,354,622,052,000 wei (0.00432 ETH), which is enough for these calls, so no balance
override was needed. `eth_simulateV1` ran with `validation: false` and `traceTransfers: true`.

**P1: 0.00001 ETH `buy` on the LABRAT curve → reverts `CurveGraduated()`**
```
eth_call {from: 0x4C2661717B97cd23aa87Fe29fE0C50CFf2CBb893, to: 0x174E4Cc2A44811Ed85eB2589C119B723a71AE024, value: 0x9184e72a000,
 data: 0x59a87bc1000000000000000000000000000000000000000000000000000009184e72a00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004c2661717b97cd23aa87fe29fe0c50cff2cbb893}
-> execution reverted, data 0x025ac17e
```

**P2: the same calldata on a curve that is still live** (`0x2E6A4c9E6b7872d7777056c605034CAc358E7cFA`, token
`0x63626b86…`, phase 0, feeBps 100, creatorTaxBps 0) **→ 2733.510505281895614172 tokens**
```
eth_call {from: launch wallet, to: 0x2E6A4c9E6b7872d7777056c605034CAc358E7cFA, value: 0x9184e72a000, data: <same as P1>}
-> 0x0000000000000000000000000000000000000000000000942f1429cb65ed46dc   (= constant-product quote, to the wei)
```

**P3: 0.00001 ETH LABRAT pool buy through the router → 349.132310919230143372 LABRAT**

Quote (eth_call to the quoter):
```
to 0xe202BB8dd524eE9C5E679e5B5809f7A373a982Ef data
0xaa9d21cb00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000aca07fe3bc5ff3e7501ca1dccfcd937fd710680d000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000c8000000000000000000000000e5e702641ea86f4ae6cc3cdaed2b886f976be0440000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000000000000000000000000009184e72a00000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000000000000000000000000000000000000000
-> amountOut 349132310919230143372, gasEstimate 84989
```
The buy, with amountOutMinimum = the quote and deadline 1790287948 (`0x6ab5a04c`, pinned-block time + 1200 s). It
succeeds as an eth_call, returns `0x`, and reverts `V4TooLittleReceived` with minimum + 1. `eth_estimateGas` gives
163,889.
```
eth_call {from: 0x4C2661717B97cd23aa87Fe29fE0C50CFf2CBb893, to: 0x8876789976dEcBfCbBbe364623C63652db8C0904, value: 0x9184e72a000, data:
0x3593564c000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000a0000000000000000000000000000000000000000000000000000000006ab5a04c00000000000000000000000000000000000000000000000000000000000000011000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000360000000000000000000000000000000000000000000000000000000000000004000000000000000000000000000000000000000000000000000000000000000800000000000000000000000000000000000000000000000000000000000000003060c0f00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000260000000000000000000000000000000000000000000000000000000000000018000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000aca07fe3bc5ff3e7501ca1dccfcd937fd710680d000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000c8000000000000000000000000e5e702641ea86f4ae6cc3cdaed2b886f976be0440000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000000000000000000000000009184e72a000000000000000000000000000000000000000000000000012ed2f22ffaff23b8c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000140000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000009184e72a0000000000000000000000000000000000000000000000000000000000000000040000000000000000000000000aca07fe3bc5ff3e7501ca1dccfcd937fd710680d000000000000000000000000000000000000000000000012ed2f22ffaff23b8c}
-> 0x (success)
```
The same call with amountOutMinimum 0 in `eth_simulateV1` (status 1, 156,061 gas used) produced these logs:
- ETH: launch wallet → router, 1e13
- PoolManager `Swap`
- LABRAT: PoolManager → hook, 10797906523275159072
- ETH: router → PoolManager, 1e13
- **LABRAT: PoolManager → launch wallet, 349132310919230143372**

**P4: claimable creator fees**
```
eth_call {to: 0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e, data: 0x70a082310000000000000000000000004c2661717b97cd23aa87fe29fe0c50cff2cbb893}
-> 0x000000000000000000000000000000000000000000000000103021e0243288e4 = 1166469550052640996 wei = 1.16647 ETH
```
Not yet in the escrow, still pending in the hook for the LABRAT pool:
```
eth_call {to: 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044, data: 0xc8eaa792b0eb2633c73b39d2832643b62f54d99b71af752cf672df6b898a2210b8c2769d0000000000000000000000000000000000000000000000000000000000000000}
-> 173807151118250 wei (ETH side)
eth_call {to: 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044, data: 0xc8eaa792b0eb2633c73b39d2832643b62f54d99b71af752cf672df6b898a2210b8c2769d000000000000000000000000aca07fe3bc5ff3e7501ca1dccfcd937fd710680d}
-> 562977381902582544480 (562.98 LABRAT, swapped to ETH at the next sweep)
```
A simulated claim, `eth_simulateV1 [{from: launch wallet, to: FeeEscrow, data: 0x4e71d92d}, {…balanceOf…}]`:
status 1, it returns 1166469550052640996, emits an ETH transfer of 1166469550052640996 from the escrow to the launch
wallet and `Claimed(launch wallet, 1166469550052640996)`, and `balanceOf` afterwards is 0. `eth_estimateGas` for
`claim()` is 43,495. `claim(1e15)` leaves 1165469550052640996. None of this was sent.

## 4. What this means for the engine (design notes, nothing built)

- **Route:** before each buy, check the phase (1b). For LABRAT today that means the router path in 1c. Keep the
  curve path only for completeness.
- **Per-hit size versus gas:** one router buy is about 164k gas, about 0.0000072 ETH at today's gas price. Sending a
  0.00001 ETH buy for every hit would spend about 42% of each hit's total cost on gas. Batching hits (for example
  "every N hits, or every T seconds, buy N × size") would keep the rule "each hit adds X to the buyback" and cut the
  gas share. That is a product choice for the owner.
- **Slippage and deadline:** quote with the quoter at send time, set `amountOutMinimum` = quote × (1 − s), and set the
  deadline to now + a few minutes.
- **Funding:** fees arrive in the FeeEscrow as ETH. They have to be claimed (a tx from the launch wallet) before they
  can pay for buys. The launch wallet itself holds only 0.00432 ETH. Claiming and buying are both transactions, and
  the owner has said not to do either yet.
- **What the buyback costs and returns:** 3% of every pool buy goes to the hook. 2 of those 3 points come back to the
  launch wallet as creator tax (after the operator's swap to ETH).
- **pons has its own buyback switch.** Factory `setBuybackEnabled(address token, bool)` (`0xb18f1db1`) succeeds as an
  eth_call from the launch wallet and reverts `NotBuybackController()` (`0x377dfc9a`) from a stranger. The related
  getters are `buybackBurnBps()` = 5000, `buybackCreatorRecipient()` = launch wallet, and a `buybackVault` with
  `VESTING_DURATION()` = 157,680,000 s (5 years). **I did not work out what it does** (how much of the fees it
  diverts, what gets burned, what vests to whom). I did not flip it, and it should not be flipped without reading
  pons's documentation.

## Sure vs. not sure

**Sure (reproduced on chain):**
- the curve address
- LABRAT graduated at block 71688341, and a curve buy now reverts `CurveGraduated()`
- the curve `buy` selector and types, the exact-value rule, minimum-out, recipient, return value, the `CurveBuy`
  event topic, and the refund/complete events
- the router pool-buy encoding (6-field swap struct, `0x10` + `0x060c0f`); that tokens go to `msg.sender`; that
  minimum-out and the deadline are enforced; that the quoter matches the simulated buy to the wei
- the 3% hook cut on a buy
- the FeeEscrow address, `balanceOf` / `claim()` / `claim(uint256)` selectors and behaviour, and that the fees are
  ETH
- the claimable amount (1166469550052640996 wei at block 71706358); that nothing has been claimed yet; the pending
  hook amounts

**Not sure / not established:**
- verified ABIs and source (blockscout was behind a Cloudflare challenge that I did not bypass); parameter names are
  my inference
- the meaning of the extra `uint256` in the router's swap struct, and why a 5-field call also succeeded
- phases 1 and 3
- the exact protocol / creator / buyback split formula in `FeesSwept`
- what pons's native buyback switch does
- whether `CreditedToken` could ever credit the launch wallet in LABRAT instead of ETH (it has not so far)
- future gas prices
- the operator's sweep schedule, which is pons's own and could change
