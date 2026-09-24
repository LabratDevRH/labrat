/* labrat site config: where the live viewer (js/live.js) connects.
 *
 * The relay (relay/relay.py) serves the public viewer socket at /live.
 *  - Site and relay on the same host (the relay with SERVE_SITE=1, locally or deployed): leave RELAY empty; the
 *    page connects to its own origin + /live.
 *  - Site on one host (Vercel) and relay on another (Railway): set RELAY to the relay's public socket, e.g.
 *      var RELAY = 'wss://<your-relay>.up.railway.app/live';
 *  - Opened as a file: the local relay on ws://localhost:4720/live.
 *  - Local preview from some other static server: add ?relay=ws://localhost:4720/live to the page URL (honoured
 *    only when the page itself is on localhost, so nobody can point the public site at another relay).
 * A value set on window.LABRAT_RELAY before this file loads wins.
 */
(function () {
  var RELAY = 'wss://labrat-relay-production.up.railway.app/live';   // the public relay (Railway)

  if (window.LABRAT_RELAY) return;
  var host = location.hostname;
  var file = location.protocol === 'file:';
  var local = file || host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '::1';
  var override = null;
  if (local) {
    try {
      var q = new URLSearchParams(location.search).get('relay');
      if (q && /^wss?:\/\//i.test(q)) override = q;
    } catch (e) { /* old browser: no override */ }
  }
  if (override) window.LABRAT_RELAY = override;
  else if (RELAY && !local) window.LABRAT_RELAY = RELAY;   // local previews keep using their own relay
  else if (file) window.LABRAT_RELAY = 'ws://localhost:4720/live';
  else window.LABRAT_RELAY = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/live';
})();
