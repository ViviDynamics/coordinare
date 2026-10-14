// 151: the System health card. The server builds one row per subsystem (system_health in
// the snapshot); these functions turn it into markup. The card is one line unless a
// subsystem is degraded or down, and opens itself only then. Unknown and not-monitored rows
// are counted on the line, neutrally. Every value goes
// through esc(). Freshness is marked up for refreshElapsedReadouts, so it keeps counting.

var SYSTEM_HEALTH_WORDS = {healthy: 'healthy', degraded: 'degraded', down: 'down', unknown: 'not checked yet', not_monitored: 'not monitored'};

// The one-line summary: its text, its tone, and whether the card opens by itself.
function systemHealthSummary(sh) {
  var rows = Array.isArray(sh.rows) ? sh.rows : [];
  if (rows.length === 0) return {tone: 'note', open: false, text: 'No systems reported'};
  var countOf = function(status) { return rows.filter(function(r) { return r.status === status; }).length; };
  var notMonitored = countOf('not_monitored');
  var unknown = countOf('unknown');
  // Neutral counts for rows that are not problems: they are named on the line but never open the card.
  var notes = (unknown > 0 ? ' · ' + unknown + ' awaiting first check' : '') +
    (notMonitored > 0 ? ' · ' + notMonitored + ' not monitored' : '');
  var problems = rows.filter(function(r) { return r.status === 'down' || r.status === 'degraded'; });
  if (problems.length > 0) {
    var named = problems.map(function(r) { return r.name + ' (' + (SYSTEM_HEALTH_WORDS[r.status] || r.status) + ')'; }).join(', ');
    var verb = problems.length === 1 ? ' system needs' : ' systems need';
    var tone = countOf('down') > 0 ? 'down' : 'degraded';
    return {tone: tone, open: true, text: problems.length + verb + ' attention: ' + named + notes};
  }
  if (unknown > 0) {
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
  if (row.checked_at) {
    var label = row.fresh_label || 'checked';
    html += ' <span class="sh-fresh" data-elapsed-since="' + esc(row.checked_at) + '" data-elapsed-prefix="' + esc(label + ' ') +
      '" data-elapsed-suffix=" ago">' + esc(label + ' ' + (fmtAge(row.checked_at) || '-') + ' ago') + '</span>';
  }
  if (row.last_success_at) {
    html += ' <span class="sh-fresh" data-elapsed-since="' + esc(row.last_success_at) +
      '" data-elapsed-prefix="last success " data-elapsed-suffix=" ago">' +
      esc('last success ' + (fmtAge(row.last_success_at) || '-') + ' ago') + '</span>';
  }
  if (row.reason) html += '<div class="sh-reason">' + esc(row.reason) + '</div>';
  return html + '</div>';
}

// The whole card: the summary line, and the rows inside it.
function systemHealthHtml(sh) {
  var sum = systemHealthSummary(sh);
  var rows = (Array.isArray(sh.rows) ? sh.rows : []).map(systemHealthRowHtml).join('');
  return '<details' + (sum.open ? ' open' : '') + '>' +
    '<summary class="sh-summary sh-tone-' + esc(sum.tone) + '" style="cursor:pointer;list-style:none;padding:2px 0">' +
      '<span class="sh-dot" aria-hidden="true">&#9679;</span> ' + esc(sum.text) +
    '</summary>' +
    '<div class="sh-list">' + rows + '</div>' +
  '</details>';
}
