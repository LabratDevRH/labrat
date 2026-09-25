/* labrat site: rat buybacks panel. statusUrl is the buyback engine's public status JSON.
   Local previews only: ?bb=http://localhost:<port>/<path> points the panel at a local engine (honoured only when the
   page itself is on localhost, so nobody can point the public site at another status). */
window.LABRAT_BUYBACK = { enabled: true, statusUrl: 'https://labrat-buyback-production.up.railway.app/status' };
(function () {
  var h = location.hostname;
  if (h !== 'localhost' && h !== '127.0.0.1') return;
  try {
    var q = new URLSearchParams(location.search).get('bb');
    if (q && /^http:\/\/(localhost|127\.0\.0\.1):\d{2,5}\/[\w\/.-]*$/.test(q)) window.LABRAT_BUYBACK.statusUrl = q;
  } catch (e) { /* old browser: no override */ }
})();
