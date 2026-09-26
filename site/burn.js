/* labrat site: the rat burns panel (/burn). statusUrl is the burn engine's public status JSON (live/burn.py /status).
   It must match the Railway service the burn engine runs on; the panel renders dashes while it is unreachable.
   Local previews only: ?burn=http://localhost:<port>/<path> points the panel at a local engine (honoured only when the
   page itself is on localhost, so nobody can point the public site at another status). */
window.LABRAT_BURN = { enabled: true, statusUrl: 'https://labrat-burn-production.up.railway.app/status' };
(function () {
  var h = location.hostname;
  if (h !== 'localhost' && h !== '127.0.0.1') return;
  try {
    var q = new URLSearchParams(location.search).get('burn');
    if (q && /^http:\/\/(localhost|127\.0\.0\.1):\d{2,5}\/[\w\/.-]*$/.test(q)) window.LABRAT_BURN.statusUrl = q;
  } catch (e) { /* old browser: no override */ }
})();
