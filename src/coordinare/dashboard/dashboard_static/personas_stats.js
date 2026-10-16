// 183: read only persona observability, entirely from /api/stats.
var PERSONA_STATS_REFRESH_MS = 30000;
var PERSONA_STATS_WINDOWS = ['24h', '7d', '30d'];
var PERSONA_OUTCOMES = {success: 'Success', failure: 'Changes requested', blocked: 'Blocked', system_error: 'System error', cancelled: 'Cancelled', completed_without_verdict: 'Completed without verdict', unknown: 'Not recorded'};
var _personaStats = {role: '', window: '7d', scope: '', seq: 0, loadedAt: 0, loading: false, body: null};
var _personaStatDefinitionSeq = 0;

function personaStatsQuery(role, windowName, scope) {
  return '/api/stats?group_by=symphony&agent=' + encodeURIComponent(role) + '&window=' + encodeURIComponent(windowName) + (scope ? '&symphony=' + encodeURIComponent(scope) : '');
}

function personaStatText(value, kind) {
  if (value == null) return 'not recorded';
  if (kind === 'rate') return (value * 100).toFixed(1).replace(/\.0$/, '') + '%';
  if (kind === 'duration') return fmtDuration(value);
  if (kind === 'cost') return usageMoney(value);
  return usageCount(value);
}

function personaStatTip(body, key, description) {
  var definition = (description ? description + ' ' : '') + ((body.definitions || {})[key] || key);
  return definition + ' Window: ' + body.window + ', run starts ' + (body.start || 'all retained') + ' to ' + body.end + '.';
}

function personaStatValue(body, measures, key, kind, value, description) {
  var partialUsage = (kind === 'tokens' || kind === 'cost') && (body.partial || measures.missing_usage_runs > 0);
  var text = partialUsage ? 'partial' : personaStatText(value, kind);
  if (body.partial && !partialUsage) text += ' (partial)';
  var tip = personaStatTip(body, key, description);
  var definitionId = 'persona-stat-definition-' + (++_personaStatDefinitionSeq);
  return '<span data-metric="' + esc(key) + '" title="' + esc(tip) + '" tabindex="0" aria-describedby="' + definitionId + '">' + esc(text) + '</span><span class="persona-stat-definition" id="' + definitionId + '">' + esc(tip) + '</span>';
}

function personaStatsTiles(body) {
  var totals = body.totals;
  var duration = totals.duration_seconds || {};
  var tiles = [
    ['Runs', 'runs', 'count', totals.runs, 'Recorded runs started in this cohort.'],
    ['Success rate', 'success_rate', 'rate', totals.success_rate, 'Passing verdicts divided by pass or fail verdicts; no verdict means not recorded.'],
    ['First pass rate', 'first_pass_rate', 'rate', totals.first_pass_rate, 'Resolved card and stage pairs passing on their only retained attempt.'],
    ['Median duration', 'duration_seconds', 'duration', duration.median, 'Median recorded run duration.'],
    ['p90 duration', 'duration_seconds', 'duration', duration.p90, '90th percentile recorded run duration.'],
    ['Tokens', 'tokens', 'tokens', (totals.tokens || {}).total, 'Recorded input, output, cache read and cache write tokens.'],
    ['Cost', 'cost', 'cost', (totals.cost || {}).usd, 'Total priced USD for the selected runs.'],
    ['Cost per run', 'cost_per_run_usd', 'cost', totals.cost_per_run_usd, 'Total priced USD divided by all selected runs.']
  ];
  return '<dl class="persona-stats-tiles">' + tiles.map(function(tile) {
    return '<div><dt>' + esc(tile[0]) + '</dt><dd>' + personaStatValue(body, totals, tile[1], tile[2], tile[3], tile[4]) + '</dd></div>';
  }).join('') + '</dl>';
}

function personaStatsTrends(body) {
  var charts = [['Daily runs', 'runs', 'count'], ['Daily success rate', 'success_rate', 'rate'], ['Daily cost', 'cost', 'cost']];
  var edits = (body.persona_edits || []).map(function(edit) {
    return {x: timeMs(edit.changed_at), label: 'Persona edit v' + (edit.version == null ? 'not recorded' : edit.version) + ' in ' + edit.symphony + ' at ' + edit.changed_at};
  });
  (body.shared_persona_edits || []).forEach(function(edit) {
    edits.push({x: timeMs(edit.changed_at), label: 'Shared persona history edit v' + edit.version + ' at ' + edit.changed_at + ' (symphony not recorded)'});
  });
  return '<div class="persona-trends">' + charts.map(function(chart) {
    var series = (body.daily || []).map(function(day) {
      var value = chart[1] === 'cost' ? (day.cost || {}).usd : day[chart[1]];
      if (chart[1] === 'cost' && (body.partial || day.missing_usage_runs)) value = null;
      return {x: timeMs(day.day + 'T00:00:00Z'), label: day.day + ' UTC', value: value};
    });
    var usagePartial = chart[1] === 'cost' && (body.partial || (body.daily || []).some(function(day) { return day.missing_usage_runs > 0; }));
    var title = chart[0] + ' (' + body.window + ', observed UTC start days' + (body.partial || usagePartial ? ', partial' : '') + ')';
    return '<figure title="' + esc(personaStatTip(body, chart[1], chart[0])) + '"><figcaption>' + esc(title) + '</figcaption>' + statsChart(series, title, function(value) { return value == null && usagePartial ? 'partial' : personaStatText(value, chart[2]); }, edits) + '</figure>';
  }).join('') + '</div>';
}

function personaStatsSymphonies(body) {
  var runtime = body.runtime || {};
  var rows = (runtime.symphonies || []).slice();
  (body.groups || []).forEach(function(group) {
    if (!rows.some(function(row) { return row.symphony === group.key; })) rows.push({symphony: group.key});
  });
  return '<h3>Where it runs</h3><p>' + (runtime.stage ? stageLabel(runtime.stage) + ' stage' : 'No pipeline stage. Stage verdict success and first pass rates do not apply.') + '</p>' +
    (runtime.stage && runtime.verdict_applicable === false ? '<p>Success and first pass rates require stage verdicts; completion alone has no verdict.</p>' : '') +
    '<div class="persona-table-scroll" tabindex="0" role="region" aria-label="Statistics table"><table><thead><tr><th>Symphony</th><th>Runs</th><th>Success rate</th><th>Cost</th><th>Persona</th><th>Model</th><th>Mode</th><th>Performer host</th></tr></thead><tbody>' + rows.map(function(row) {
      var measures = (body.groups || []).filter(function(group) { return group.key === row.symphony; })[0];
      var figures = measures ? '<td>' + personaStatValue(body, measures, 'runs', 'count', measures.runs) + '</td><td>' + personaStatValue(body, measures, 'success_rate', 'rate', measures.success_rate) + '</td><td>' + personaStatValue(body, measures, 'cost', 'cost', (measures.cost || {}).usd) + '</td>' : '<td colspan="3">No runs in this window</td>';
      return '<tr><th scope="row">' + esc(row.symphony || 'not recorded') + '</th>' + figures + '<td>' + esc(row.persona_override ? 'Override' : 'Inherited') + '</td><td>' + esc(row.model || 'not recorded') + '</td><td>' + esc(row.mode || 'not recorded') + '</td><td title="Configured eligible hosts; the pool chooses a host at dispatch">' + esc((row.hosts || []).join(', ') || 'not recorded') + '</td></tr>';
    }).join('') + '</tbody></table></div>';
}

function personaStatsActivity(body) {
  var activity = (body.runtime || {}).activity || [];
  return '<h3>Current activity</h3>' + (activity.length ? '<ul>' + activity.map(function(row) {
    var path = runPathFor(row.symphony, row.card, row.run);
    return '<li>Running on <a href="' + esc(path) + '" data-persona-link>' + esc(row.card_title || row.card) + '</a> in ' + esc(row.symphony) + '</li>';
  }).join('') + '</ul>' : '<p>No running session observed.</p>');
}

function personaStatsRecent(body, role, scope) {
  var path = '/history?symphony=' + encodeURIComponent(scope) + '&h.agent=' + encodeURIComponent(role);
  return '<h3>Recent runs</h3><p><a href="' + esc(path) + '" data-persona-link>History for this agent</a></p>' +
    ((body.recent_runs || []).length ? '<div class="persona-table-scroll" tabindex="0" role="region" aria-label="Statistics table"><table><thead><tr><th>Started</th><th>Card</th><th>Symphony</th><th>Outcome</th><th>Duration</th><th>Cost</th></tr></thead><tbody>' + body.recent_runs.map(function(row) {
      return '<tr><td>' + timeHtml(row.started_at) + '</td><td><a href="' + esc(runPathFor(row.symphony, row.card, row.run)) + '" data-persona-link title="Open transcript">' + esc(row.card_title || row.card) + '</a></td><td>' + esc(row.attributed_symphony || row.symphony) + '</td><td>' + esc(PERSONA_OUTCOMES[row.outcome] || 'Not recorded') + '</td><td>' + personaStatValue(body, row, 'duration_seconds', 'duration', row.duration_seconds) + '</td><td>' + personaStatValue(body, row, 'cost', 'cost', (row.cost || {}).usd) + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '<p>No recent runs in this window.</p>');
}

function personaStatsVersions(body) {
  return '<details><summary>Persona versions</summary><p>Recorded versions only. Dashed trend markers show recorded edit times, including shared persona history where symphony attribution was not recorded. Versions with different content or symphonies are separate.</p>' +
    ((body.persona_versions || []).length ? '<div class="persona-table-scroll" tabindex="0" role="region" aria-label="Statistics table"><table><thead><tr><th>Version</th><th>Symphony</th><th>Runs</th><th>Success rate</th><th>Median duration</th><th>Cost</th></tr></thead><tbody>' + body.persona_versions.map(function(row) {
      return '<tr><th title="' + esc(row.sha256 || 'Content hash not recorded') + '">' + esc(row.version == null ? 'not recorded' : 'v' + row.version) + '</th><td>' + esc(row.symphony) + '</td><td>' + personaStatValue(body, row, 'runs', 'count', row.runs) + '</td><td>' + personaStatValue(body, row, 'success_rate', 'rate', row.success_rate) + '</td><td>' + personaStatValue(body, row, 'duration_seconds', 'duration', (row.duration_seconds || {}).median) + '</td><td>' + personaStatValue(body, row, 'cost', 'cost', (row.cost || {}).usd) + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '<p>No recorded persona versions in this window.</p>') + '</details>';
}

function personaStatsHtml(body, role, scope) {
  var totals = body.totals || {};
  var coverage = body.coverage || {};
  var notice = body.partial ? '<p role="status">Statistics are partial: ' + esc((body.partial_reasons || []).join(', ')) + '.</p>' : '';
  if (totals.missing_usage_runs) notice += '<p>Usage is partial: some runs have no complete usage records.</p>';
  if (coverage.unattributed_observations) notice += '<p>Some outcomes could not be attributed to a run; rates cover recorded verdicts only.</p>';
  var outcomes = Object.keys(PERSONA_OUTCOMES).map(function(key) { return {label: PERSONA_OUTCOMES[key], value: (totals.outcomes || {})[key] || 0}; });
  var reasons = (totals.failure_reasons || []).map(function(reason) {
    return '<li>' + esc(reason.reason) + ': ' + personaStatValue(body, totals, 'failure_reasons', 'count', reason.runs, 'Runs with this recorded block, error or failing verdict reason.') + '</li>';
  });
  return '<p class="muted">Observed ' + timeHtml(body.end) + '. Run start window: ' + esc(body.window) + '.</p>' + notice + (totals.runs ? personaStatsTiles(body) + '<h3>Outcomes</h3>' + statsBars(outcomes, 'Recorded outcomes (' + body.window + ')' + (body.partial ? ', partial' : '')) + personaStatsTrends(body) : '<p class="empty-state">No runs in this window' + (body.partial ? ' among the available observations' : '') + '.</p>') +
    personaStatsSymphonies(body) + personaStatsActivity(body) + personaStatsRecent(body, role, scope) + '<h3>Common failure reasons</h3>' + (reasons.length ? '<ul>' + reasons.join('') + '</ul>' : '<p>No recorded block or error reasons in this window.</p>') + personaStatsVersions(body);
}

function personaStatsScope() {
  var scoped = currentScopedState();
  if (!scoped) return null;
  var names = symphonyNames(scoped);
  return names.length === 1 ? names[0] : '';
}

// The read-only workspace owns one statistics view, moved into its selected tab panel.
// Config uses the same selector without mounting statistics or installing this callback.
function personaStatsMount(root) {
  if (!root || root.getAttribute('data-persona-context') !== 'read') return;
  var controls = '<section id="persona-stats" aria-label="Persona statistics"><div class="persona-stats-controls"><label>Window <select id="persona-stats-window">' + PERSONA_STATS_WINDOWS.map(function(name) {
    return '<option value="' + esc(name) + '"' + (name === _personaStats.window ? ' selected' : '') + '>' + esc(name) + '</option>';
  }).join('') + '</select></label><button type="button" class="action-btn" id="persona-stats-refresh">Refresh</button></div><div id="persona-stats-content"></div></section>';
  root.insertAdjacentHTML('beforeend', controls);
  var stats = document.getElementById('persona-stats');
  root.onPersonaSelect = function(role) {
    var panel = document.getElementById('read-persona-' + role);
    if (!panel) return;
    panel.insertBefore(stats, panel.firstChild);
    _personaStats.role = role;
    personaStatsRefresh(true);
  };
  document.getElementById('persona-stats-window').onchange = function() { _personaStats.window = this.value; personaStatsRefresh(true); };
  document.getElementById('persona-stats-refresh').onclick = function() { personaStatsRefresh(true); };
  stats.onclick = function(event) {
    var link = event.target.closest('[data-persona-link]');
    if (link) navigate(event, link.getAttribute('href'));
  };
}

function personaStatsRefresh(force) {
  if (location.pathname !== '/personas') return;
  var target = document.getElementById('persona-stats-content');
  if (!target) return;
  var scope = personaStatsScope();
  if (scope === null) { target.innerHTML = '<p role="status">Waiting for dashboard state...</p>'; return; }
  var signature = [_personaStats.role, _personaStats.window, scope].join('|');
  if (!force && signature === _personaStats.signature && (_personaStats.loading || Date.now() - _personaStats.loadedAt < PERSONA_STATS_REFRESH_MS)) return;
  _personaStats.signature = signature;
  _personaStats.scope = scope;
  var seq = ++_personaStats.seq;
  _personaStats.loading = true;
  _personaStats.loadedAt = Date.now();
  target.innerHTML = '<p role="status">Loading statistics...</p>';
  var role = _personaStats.role;
  fetch(personaStatsQuery(role, _personaStats.window, scope), {credentials: 'same-origin'}).then(function(response) {
    if (!response.ok) throw new Error('stats unavailable');
    return response.json();
  }).then(function(body) {
    if (seq !== _personaStats.seq || location.pathname !== '/personas') return;
    _personaStats.loading = false;
    _personaStats.body = body;
    target.innerHTML = personaStatsHtml(body, role, scope);
  }).catch(function() {
    if (seq !== _personaStats.seq || location.pathname !== '/personas') return;
    _personaStats.loading = false;
    target.innerHTML = '<p role="alert">Statistics could not be loaded. Use Refresh to try again.</p>';
  });
}
