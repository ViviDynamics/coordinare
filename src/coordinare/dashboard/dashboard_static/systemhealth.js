// 151: the System health card. The server builds one row per subsystem (system_health in
// the snapshot); these functions turn it into markup. The card is one line unless a
// subsystem is degraded or down, and opens itself only then. Unknown, stale and
// not-monitored rows are counted on the line, neutrally. Every value goes
// through esc(). Freshness is marked up for refreshElapsedReadouts, so it keeps counting.

var SYSTEM_HEALTH_WORDS = {healthy: 'healthy', degraded: 'degraded', down: 'down', unknown: 'not checked yet', stale: 'stale', not_monitored: 'not monitored'};

// The user's own open or closed choice. It survives re-renders, and is dropped when the
// overall status changes, because a change in status is news that should open the card.
var SYSTEM_HEALTH_CHOICE = {overall: null, open: null};

// Whether the card is open: the user's choice if they made one for this overall status,
// otherwise what the summary says.
function systemHealthOpen(sh, sum) {
  if (SYSTEM_HEALTH_CHOICE.overall !== sh.overall) {
    SYSTEM_HEALTH_CHOICE.overall = sh.overall;
    SYSTEM_HEALTH_CHOICE.open = null;
  }
  return SYSTEM_HEALTH_CHOICE.open === null ? sum.open : SYSTEM_HEALTH_CHOICE.open;
}

// Called when the user opens or closes the card.
function systemHealthRemember(open) {
  SYSTEM_HEALTH_CHOICE.open = !!open;
}

// The one-line summary: its text, its tone, and whether the card opens by itself.
function systemHealthSummary(sh) {
  var rows = Array.isArray(sh.rows) ? sh.rows : [];
  if (rows.length === 0) return {tone: 'note', open: false, text: 'No systems reported'};
  var countOf = function(status) { return rows.filter(function(r) { return r.status === status; }).length; };
  var notMonitored = countOf('not_monitored');
  var unknown = countOf('unknown');
  var stale = countOf('stale');
  // Neutral counts for rows that are not problems: they are named on the line but never open the card.
  var notes = (unknown > 0 ? ' · ' + unknown + ' awaiting first check' : '') +
    (stale > 0 ? ' · ' + stale + ' stale' : '') +
    (notMonitored > 0 ? ' · ' + notMonitored + ' not monitored' : '');
  var problems = rows.filter(function(r) { return r.status === 'down' || r.status === 'degraded'; });
  if (problems.length > 0) {
    var named = problems.map(function(r) { return r.name + ' (' + (SYSTEM_HEALTH_WORDS[r.status] || r.status) + ')'; }).join(', ');
    var verb = problems.length === 1 ? ' system needs' : ' systems need';
    var tone = countOf('down') > 0 ? 'down' : 'degraded';
    return {tone: tone, open: true, text: problems.length + verb + ' attention: ' + named + notes};
  }
  if (unknown > 0 || stale > 0) {
    return {tone: 'note', open: false, text: 'No problems seen' + notes};
  }
  if (notMonitored > 0) {
    return {tone: 'note', open: false, text: 'All monitored systems healthy' + notes};
  }
  return {tone: 'healthy', open: false, text: 'All systems healthy'};
}

// One subsystem: its status in words and in colour, its summary, how fresh it is, and why.
function systemHealthRowHtml(row) {
  var status = String(row.status || 'unknown');
  var word = SYSTEM_HEALTH_WORDS[status] || status;
  var html = '<div class="sh-row sh-' + esc(status) + '">' +
    '<span class="sh-dot" aria-hidden="true">&#9679;</span>' +
    '<span class="sh-name">' + esc(row.name) + '</span>' +
    '<span class="sh-word">' + esc(word) + '</span>' +
    '<span class="sh-summary">' + esc(row.summary || '') + '</span>';
  // 157: the ages are timeHtml times: the relative text, the exact time and zone on hover.
  if (row.checked_at) {
    var label = row.fresh_label || 'checked';
    html += ' <span class="sh-fresh">' + esc(label) + ' ' + timeHtml(row.checked_at, '-') + '</span>';
  }
  if (row.last_success_at) {
    html += ' <span class="sh-fresh">last success ' + timeHtml(row.last_success_at, '-') + '</span>';
  }
  if (row.reason) html += '<div class="sh-reason">' + esc(row.reason) + '</div>';
  return html + '</div>';
}

// 271: cached event history statistics on one line in the existing panel.
// 358: the size and the oldest event read in human units, and a count the payload does not
// carry is left out rather than shown as a bare word.
function systemHealthEventStoreHtml(stats) {
  if (!stats) return '';
  var isCount = function(v) { return typeof v === 'number' && isFinite(v); };
  var parts = [];
  if (isCount(stats.size_bytes)) parts.push(esc(fmtBytes(stats.size_bytes)));
  if (isCount(stats.rows)) parts.push(esc(stats.rows.toLocaleString('en-US')) + ' rows');
  parts.push('oldest ' + (stats.oldest_ts ? timeHtml(stats.oldest_ts, 'unknown') : 'none'));
  if (isCount(stats.dropped)) parts.push(esc(String(stats.dropped)) + ' dropped');
  if (isCount(stats.pruned)) parts.push(esc(String(stats.pruned)) + ' pruned');
  parts.push('writer ' + esc(stats.writer_state || 'unknown') + (stats.last_error ? ': ' + esc(stats.last_error) : ''));
  parts.push('export ' + esc(String(stats.export_dropped || 0)) + ' dropped');
  parts.push(esc(String(stats.export_failed || 0)) + ' failed sends');
  if (stats.export_error) parts.push('export disabled: ' + esc(stats.export_error));
  return '<div class="sh-row">Event store: ' + parts.join(', ') + '</div>';
}

// The whole card: the summary line, and the rows inside it.
function systemHealthHtml(sh) {
  var sum = systemHealthSummary(sh);
  var open = systemHealthOpen(sh, sum);
  var rows = (Array.isArray(sh.rows) ? sh.rows : []).map(systemHealthRowHtml).join('');
  return '<details' + (open ? ' open' : '') + '>' +
    '<summary class="sh-summary sh-tone-' + esc(sum.tone) + '" style="cursor:pointer;list-style:none;padding:2px 0">' +
      '<span class="sh-dot" aria-hidden="true">&#9679;</span> ' + esc(sum.text) +
    '</summary>' +
    '<div class="sh-list">' + rows + systemHealthEventStoreHtml(sh.event_store) + '</div>' +
  '</details>';
}
