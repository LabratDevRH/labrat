/* labrat site: the main coin, $LABRAT.
 *
 * null until the rat has launched it. While it is null the page shows "Launching soon" (the logo, the name and
 * the ticker) and no address, links or numbers for the coin.
 *
 * After the launch, replace null with the coin's public facts and redeploy. The page then shows the coin card in
 * the hero and under "The launch": contract (with a copy button), pons and explorer buttons, the transaction and
 * the block. Only the fields below are read and shown.
 *
 *   window.LABRAT_COIN = {
 *     name: 'Labrat',
 *     symbol: 'LABRAT',
 *     address: '0x...',                                                    // the token contract (0x + 40 hex)
 *     tx: '0x...',                                                         // the launch transaction (0x + 64 hex)
 *     block: 0,                                                            // the block it was mined in
 *     pons: 'https://www.ponsfamily.com/launchpad/<address>',
 *     explorer: 'https://robinhoodchain.blockscout.com/token/<address>',
 *     launched: 'YYYY-MM-DD'
 *   };
 */
window.LABRAT_COIN = null;
