// 349: shared dashboard helpers, served verbatim from /static/helpers.js.
// The page pins the sha256 of these bytes into the script-tag ?v= query, so a
// redeploy that changes the file changes the URL and no browser ever renders
// a stale copy from cache.
function fmtDuration(secs) {
  if (secs === null || secs === undefined) return '—';
  return secs.toFixed(2) + 's';
}

function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

function fmtAge(iso) {
  if (!iso) return null;
  var secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

function humanPhase(phase) {
  return esc(String(phase || '').replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); }));
}
var formatPhaseLabel = humanPhase;

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function fmtBytes(b) {
  if (b == null) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(0) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}
