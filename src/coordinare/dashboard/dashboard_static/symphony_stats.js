// 184: read only statistics. Every measure comes from /api/stats; snapshot fields are display metadata.
var SYMPHONY_STATS_WINDOWS = ['24h', '7d', '30d'];
var SYMPHONY_STATS_REFRESH_MS = 30000;
var SYMPHONY_STATS_DAY_MS = 24 * 60 * 60 * 1000;
var SYMPHONY_STATS_PERCENT = 100;
var SYMPHONY_STATS_MS_PER_SECOND = 1000;
var _symphonyStatsWindow = '7d';
var _symphonyStatsRequest = 0;
var _symphonyStatsPage = '';
var _symphonyStatsLoadedAt = 0;
var _symphonyStatsLoading = false;
var _symphonyListStatsRead = null;

function symphonyStatsRevision(sym) {
  var state = sym.state || {};
  var sessions = state.active_sessions || {};
  return JSON.stringify([sym.name, sym.last_poll_at, sym.error_count, sym.enabled, sym.env_bootstrap,
    state.board_snapshot, state.todo_waits, state.session_skip_reasons,
    Object.keys(sessions).map(function(id) {
      var session = sessions[id];
      return [id, session.phase, session.slot_queued_since, (session.agent_dispatch || {}).session_id,
        session.documenting_side, session.review_wait];
    })]);
}

function symphonyStatsNumber(value) {
  return typeof value === 'number' && isFinite(value) ? String(value) : 'Not recorded';
}

function symphonyStatsPercent(value) {
  return value == null ? 'Not recorded' : Math.round(value * SYMPHONY_STATS_PERCENT) + '%';
}

function symphonyStatsDuration(value) {
  return value == null ? 'Not recorded' : fmtDuration(value);
}

function symphonyStatsMoney(value) {
  return value == null ? 'Not recorded' : '$' + value.toFixed(2);
}

function symphonyStatsUsagePartial(response, measures) {
  return !response || response.partial || !measures || measures.missing_usage_runs > 0;
}

function symphonyStatsHeadline(response, name) {
  if (!response) return {active: 'Loading', queued: 'Loading', completed: 'Loading', spend: 'Loading'};
  var health = (response.symphony_health || {})[name] || {};
  var flow = (response.card_flow || {})[name] || {};
  var measures = response.totals || {};
  if (response.group_by === 'symphony') {
    measures = (response.groups || []).filter(function(row) { return row.key === name; })[0] || null;
  }
  var spend = !measures ? 'Not recorded' : symphonyStatsUsagePartial(response, measures) ? 'partial' : symphonyStatsMoney((measures.cost || {}).usd);
  if (measures && !response.partial && !measures.missing_usage_runs && (measures.cost || {}).usd == null) spend = 'Not recorded';
  return {
    active: symphonyStatsNumber(health.cards_active), queued: symphonyStatsNumber(health.cards_queued),
    completed: symphonyStatsNumber(flow.cards_completed) + (response.partial ? ' (partial)' : ''), spend: spend
  };
}

function symphonyStatsTip(response, definition) {
  return definition + ' Window: ' + response.window + ', ' + (response.start || 'all retained') +
    ' to ' + response.end + '.';
}

function symphonyStatsMetric(response, label, value, definition) {
  return '<div class="sym-stat" title="' + esc(symphonyStatsTip(response, definition)) + '">' +
    '<span class="muted">' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>';
}

function symphonyStatsTable(response, headings, rows, definition) {
  var tip = symphonyStatsTip(response, definition);
  return '<div class="sym-stats-table" tabindex="0" role="region" aria-label="' + esc(definition) +
    '" title="' + esc(tip) + '"><table><thead><tr>' +
    headings.map(function(heading) { return '<th scope="col">' + esc(heading) + '</th>'; }).join('') +
    '</tr></thead><tbody>' + rows.map(function(row) {
      return '<tr>' + row.map(function(cell) { return '<td title="' + esc(tip) + '">' + cell + '</td>'; }).join('') + '</tr>';
    }).join('') + '</tbody></table>' + (rows.length ? '' : '<p class="muted">Not recorded in this window.</p>') + '</div>';
}

function symphonyStatsLink(href, text) {
  return '<a href="' + esc(href) + '">' + esc(text) + '</a>';
}

function symphonyStatsDaily(response, rows, field, description) {
  var day = new Date(timeMs(response.start));
  var end = timeMs(response.end);
  var values = {};
  (rows || []).forEach(function(row) { values[row.day] = field === 'cost' ? (row.cost || {}).usd : row[field]; });
  var points = [];
  while (isFinite(day.getTime()) && day.getTime() <= end) {
    var label = day.toISOString().slice(0, 10);
    points.push({x: day.getTime(), label: label, value: Object.prototype.hasOwnProperty.call(values, label) ? values[label] : 0});
    day = new Date(day.getTime() + SYMPHONY_STATS_DAY_MS);
  }
  return statsChartHtml(points, symphonyStatsTip(response, description + ' by UTC day'));
}

function symphonyStatsNowHtml(name, response, health, flow) {
  var st = health.state || {};
  var columns = flow.columns_now;
  var metrics = columns ? Object.keys(columns).map(function(column) {
    return symphonyStatsMetric(response, boardColumnLabel(column), symphonyStatsNumber(columns[column]),
      'Cards in the latest Dashboard board snapshot, column ' + column + '. Current, independent of selected window.');
  }).join('') : '<p class="muted">Board has not been checked yet.</p>';
  metrics += symphonyStatsMetric(response, 'Cards running', symphonyStatsNumber(health.cards_active), 'Cards with live performer work now.');
  metrics += symphonyStatsMetric(response, 'Queued', symphonyStatsNumber(health.cards_queued), 'Cards waiting for a performer slot now.');
  metrics += symphonyStatsMetric(response, 'Parked in review', symphonyStatsNumber(health.cards_parked), 'Cards waiting on human review without live work now.');
  metrics += symphonyStatsMetric(response, 'Card slots', symphonyStatsNumber(health.slots_in_use) + ' of ' +
    symphonyStatsNumber(health.max_concurrent_cards), 'Pipeline slots currently reserved against max_concurrent_cards.');
  var waiting = [];
  var sessions = st.active_sessions || {};
  Object.keys(st.board_snapshot || {}).forEach(function(column) {
    if (column === 'DONE') return;
    (st.board_snapshot[column] || []).forEach(function(id) {
      var session = sessions[id] || {};
      var reason = waitingReason({id: id, column: column, has_session: !!sessions[id], block_cause: session.block_cause}, st);
      if ((health.running_card_ids || []).indexOf(id) !== -1) return;
      var text = reason.text || session.review_wait_reason ||
        (column === 'IN_REVIEW' ? 'Waiting on review; reason not recorded' :
          column === 'BACKLOG' ? 'Backlog: not selected for work' : 'No progress reason recorded');
      waiting.push([symphonyStatsLink('/cards/' + encodeURIComponent(name) + '/' + encodeURIComponent(id),
        (st.board_titles || {})[id] || id), esc(text)]);
    });
  });
  return '<section class="card sym-stats-section"><h3>Now</h3><div class="sym-stats-grid">' + metrics + '</div>' +
    symphonyStatsTable(response, ['Card', 'Why it is waiting'], waiting, 'Current cards without live performer work and their recorded waiting reasons.') + '</section>';
}

function symphonyStatsSpendRows(response) {
  return (response.groups || []).map(function(row) {
    var partial = symphonyStatsUsagePartial(response, row);
    return [response.group_by === 'stage' ? stageLabel(row.key) : esc(row.key || 'Not recorded'),
      esc(partial ? 'partial' : symphonyStatsNumber((row.tokens || {}).total)),
      esc(partial ? 'partial' : symphonyStatsMoney((row.cost || {}).usd))];
  });
}

function symphonyStatsHtml(name, response, models) {
  var health = (response.symphony_health || {})[name] || {name: name};
  var flow = (response.card_flow || {})[name] || {};
  var totals = response.totals || {};
  var definitions = response.definitions || {};
  var status = symphonyStatus(health);
  var headline = symphonyStatsHeadline(response, name);
  var partial = symphonyStatsUsagePartial(response, totals);
  var metric = function(label, value, definition) { return symphonyStatsMetric(response, label, value, definition); };
  var env = symphonyEnvView(health);
  var check = (response.reliability || {})[name] || {};
  var cycles = flow.cycle_time_seconds || {};
  var last = health.last_poll_at;
  var lastAt = timeMs(last);
  var next = isFinite(lastAt) && health.enabled !== false && health.poll_interval_seconds > 0
    ? new Date(lastAt + health.poll_interval_seconds * SYMPHONY_STATS_MS_PER_SECOND).toISOString() : null;
  var header = '<header><h2>' + symphonyIdentityHtml(health) + '</h2><p>' + esc(status.label + ': ' + status.reason) + '</p>' +
    '<div class="sym-stats-grid">' + metric('Last checked', last ? fmtTime(last) : 'Not checked yet', 'Most recent successful board poll, current state.') +
    metric('Next check', next ? fmtTime(next) : health.enabled === false ? 'Disabled: no check scheduled' : 'Waiting for a webhook or first check', 'Earliest scheduled poll based on last checked plus poll interval; backoff may delay it.') +
    metric('Failed checks in a row', symphonyStatsNumber(health.error_count), 'Consecutive failed cycles now; a successful cycle resets the streak.') + '</div></header>';
  // Absolute timestamps use the common time helper, including its live relative update hook.
  if (last) header = header.replace(esc(fmtTime(last)), timeHtml(last));
  if (next) header = header.replace(esc(fmtTime(next)), timeHtml(next));
  var coverage = response.partial ? '<p class="sym-stats-coverage" role="status">partial: some retained records are unavailable or incomplete. Figures cover recorded observations only.</p>' :
    '<p class="muted">Recorded observations for this window. Older history may have been retained for less time.</p>';
  var throughput = '<section class="card sym-stats-section"><h3>Throughput</h3><div class="sym-stats-grid">' +
    metric('Cards completed', headline.completed, 'Recorded card merge completions selected by merge time.') +
    metric('PRs merged', symphonyStatsNumber(flow.prs_merged), 'Recorded merge events in the selected window.') + '</div>' +
    symphonyStatsDaily(response, flow.daily_completed, 'cards_completed', 'Completed cards') + '</section>';
  var stageRows = (response.groups || []).map(function(row) {
    return [stageLabel(row.key), esc(symphonyStatsNumber(row.runs)), esc(symphonyStatsPercent(row.success_rate)),
      esc(symphonyStatsPercent(row.first_pass_rate)), esc(symphonyStatsDuration((row.duration_seconds || {}).median))];
  });
  var flowHtml = '<section class="card sym-stats-section"><h3>Flow</h3><div class="sym-stats-grid">' +
    metric('Cycle time median', symphonyStatsDuration(cycles.median), definitions.cycle_time_seconds || 'First dispatch to merge, median.') +
    metric('Cycle time p90', symphonyStatsDuration(cycles.p90), 'First dispatch to merge, 90th percentile.') +
    metric('Human review wait', symphonyStatsDuration(flow.human_review_wait_seconds), definitions.human_review_wait_seconds || 'Total recorded human review intervals clipped to the window.') +
    metric('Revision rounds per PR', flow.revision_rounds_per_pr == null ? 'Not recorded' : flow.revision_rounds_per_pr.toFixed(2), definitions.revision_rounds_per_pr || 'Changes requested observations divided by distinct observed PRs.') + '</div>' +
    symphonyStatsTable(response, ['Stage', 'Runs', 'Success rate', 'First pass rate', 'Median duration'], stageRows,
      'Runs by start time. Success: passing verdict runs / verdict runs. First pass: resolved card and stage pairs passing on their only retained run / resolved pairs. Median: run start to transcript completion. Compare durations and retries to locate bottlenecks.') + '</section>';
  var spend = '<section class="card sym-stats-section"><h3>Spend</h3><div class="sym-stats-grid">' +
    metric('Tokens', partial ? 'partial' : symphonyStatsNumber((totals.tokens || {}).total), 'Input, output and cache tokens for runs starting in this window; incomplete usage is partial.') +
    metric('Cost', headline.spend, definitions.cost || 'Priced usage of runs starting in the window; incomplete or unpriced usage has no complete cost.') +
    metric('Cost per completed card', partial ? 'partial' : symphonyStatsMoney(flow.cost_per_completed_card_usd), 'Run start cohort cost divided by cards merged in the window. Cohorts can differ; not individual card cost.') + '</div>' +
    (partial ? '<p class="muted">Daily cost: partial</p>' : symphonyStatsDaily(response, response.daily, 'cost', 'Cost in USD')) +
    '<h4>By stage</h4>' + symphonyStatsTable(response, ['Stage', 'Tokens', 'Cost'], symphonyStatsSpendRows(response), 'Run start cohort usage split by stage.') +
    '<h4>By model</h4>' + (models ? symphonyStatsTable(models, ['Dispatch model', 'Tokens', 'Cost'], symphonyStatsSpendRows(models),
      'Usage split by dispatch model; actual usage models are priced individually.') : '<p class="muted">Model statistics unavailable. Reload to retry.</p>') + '</section>';
  var errors = (check.recent_errors || []).map(function(row) { return [timeHtml(row.at), esc(row.reason)]; });
  var reliabilityHtml = '<section class="card sym-stats-section"><h3>Reliability</h3><div class="sym-stats-grid">' +
    metric('Board check success rate', symphonyStatsPercent(check.success_rate), 'Successful observed upstream board polls / observed polls. Cache hits, deferred polls and restart-reconcile polls are not counted. Legacy checks were not recorded.') +
    metric('Board checks observed', symphonyStatsNumber(check.checks), 'Actual board poll observations retained in this window; recording begins with this release.') +
    metric('Performer system errors', symphonyStatsNumber((totals.outcomes || {}).system_error), 'Runs starting in the window whose latest attributed outcome is system_error.') + '</div>' +
    '<h4>Recent board errors</h4>' + symphonyStatsTable(response, ['When', 'Reason'], errors, 'Most recent failed board polls in this window.') +
    ((health.state || {}).last_error ? '<p>Last cycle error: ' + esc(health.state.last_error) + ' (time not recorded)</p>' : '') +
    '<h4>Env cache and bootstrap</h4><p>' + esc(env.label + ': ' + env.reason) + '</p>' +
    symphonyBootstrapHtml(health.env_bootstrap, name) + '</section>';
  var runs = (response.recent_runs || []).map(function(row) {
    return [symphonyStatsLink('/runs/' + [row.transcript_symphony || row.symphony, row.card, row.run].map(encodeURIComponent).join('/'), row.run),
      symphonyStatsLink('/cards/' + encodeURIComponent(row.symphony) + '/' + encodeURIComponent(row.card), row.card),
      stageLabel(row.stage), esc(vocabularyLabel(row.outcome)), timeHtml(row.started_at)];
  });
  var cards = (flow.recent_completed || []).map(function(row) {
    return [symphonyStatsLink('/cards/' + encodeURIComponent(name) + '/' + encodeURIComponent(row.card), row.card), timeHtml(row.merged_at),
      symphonyStatsLink('/history?h.symphony=' + encodeURIComponent(name) + '&h.card=' + encodeURIComponent(row.card), 'Runs and transcripts')];
  });
  return header + coverage + symphonyStatsNowHtml(name, response, health, flow) + throughput + flowHtml + spend + reliabilityHtml + symphonyStatsActionsHtml(health) +
    '<section class="card sym-stats-section"><h3>Recent runs</h3>' + symphonyStatsTable(response, ['Transcript', 'Card timeline', 'Stage', 'Outcome', 'Started'], runs, 'Last 10 retained runs starting in the selected window, newest first.') +
    '<h3>Recent completed cards</h3>' + symphonyStatsTable(response, ['Card timeline', 'Merged', 'History'], cards, 'Last 10 recorded card completions in the selected window, newest first.') +
    '<p>' + symphonyStatsLink('/history?h.symphony=' + encodeURIComponent(name), 'History for this symphony') + '</p></section>';
}

function symphonyStatsFetch(query) {
  return fetch('/api/stats?' + query, {credentials: 'same-origin'}).then(function(result) {
    if (!result.ok) throw new Error('Statistics unavailable');
    return result.json();
  });
}

function symphonyStatsScopeAllows(name) {
  var scoped = currentScopedState();
  if (!scoped) return false;
  var choice = scoped ? currentSymphonyChoice(scoped) : '';
  return !choice || choice === name;
}

function loadSymphonyStatsDetail(name, el, force, revision) {
  var key = name + ':' + _symphonyStatsWindow + ':' + (revision || '');
  if (!symphonyStatsScopeAllows(name)) {
    _symphonyStatsRequest += 1;
    _symphonyStatsLoading = false;
    _symphonyStatsPage = '';
    el.innerHTML = '<p class="empty-state">Choose All symphonies or ' + esc(name) + ' in the header to view this symphony.</p>';
    return;
  }
  if (!force && _symphonyStatsPage === key && (_symphonyStatsLoading || Date.now() - _symphonyStatsLoadedAt < SYMPHONY_STATS_REFRESH_MS)) return;
  _symphonyStatsPage = key;
  _symphonyStatsLoading = true;
  var request = ++_symphonyStatsRequest;
  var options = SYMPHONY_STATS_WINDOWS.map(function(window) {
    return '<option value="' + esc(window) + '"' + (window === _symphonyStatsWindow ? ' selected' : '') + '>' + esc(window) + '</option>';
  }).join('');
  if (force || !document.getElementById('sym-stats-window') || el.getAttribute('data-stats-detail') !== name) {
    el.setAttribute('data-stats-detail', name);
    el.innerHTML = '<a href="/symphonies">All symphonies</a><div class="sym-stats-toolbar"><label for="sym-stats-window">Statistics window</label> ' +
      '<select id="sym-stats-window">' + options + '</select></div><div id="sym-stats-content" aria-live="polite">Loading statistics...</div>';
    document.getElementById('sym-stats-window').addEventListener('change', function(event) {
      _symphonyStatsWindow = event.target.value;
      loadSymphonyStatsDetail(name, el, true, revision);
    });
  }
  var query = 'window=' + encodeURIComponent(_symphonyStatsWindow) + '&symphony=' + encodeURIComponent(name);
  Promise.all([
    symphonyStatsFetch(query + '&group_by=stage'),
    symphonyStatsFetch(query + '&group_by=model').catch(function() { return null; })
  ]).then(function(results) {
    if (request !== _symphonyStatsRequest) return;
    _symphonyStatsLoading = false;
    if (location.pathname !== '/symphonies/' + encodeURIComponent(name) || !symphonyStatsScopeAllows(name)) {
      _symphonyStatsPage = '';
      return;
    }
    var content = document.getElementById('sym-stats-content');
    if (content) {
      content.innerHTML = symphonyStatsHtml(name, results[0], results[1]);
      wireSymphonyStatsActions(name);
    }
    _symphonyStatsLoadedAt = Date.now();
  }).catch(function() {
    if (request !== _symphonyStatsRequest) return;
    _symphonyStatsLoading = false;
    var content = document.getElementById('sym-stats-content');
    if (content) content.innerHTML = '<p role="alert">Statistics unavailable. Reload to retry.</p>';
  });
}

function loadSymphonyListStats(el, choice, revision) {
  var request = ++_symphonyStatsRequest;
  _symphonyStatsPage = '';
  _symphonyStatsLoading = false;
  var query = 'group_by=symphony&window=7d' + (choice ? '&symphony=' + encodeURIComponent(choice) : '');
  if (!_symphonyListStatsRead || _symphonyListStatsRead.query !== query || _symphonyListStatsRead.revision !== revision ||
      Date.now() - _symphonyListStatsRead.at >= SYMPHONY_STATS_REFRESH_MS) {
    _symphonyListStatsRead = {query: query, revision: revision, at: Date.now(), promise: symphonyStatsFetch(query)};
  }
  _symphonyListStatsRead.promise.then(function(response) {
    if (request !== _symphonyStatsRequest || location.pathname !== '/symphonies') return;
    el.querySelectorAll('[data-stats-symphony]').forEach(function(row) {
      var name = row.getAttribute('data-stats-symphony');
      if (symphonyRowHidden(name, choice)) return;
      var headline = symphonyStatsHeadline(response, name);
      row.querySelectorAll('[data-stats-measure]').forEach(function(cell) {
        cell.textContent = headline[cell.getAttribute('data-stats-measure')];
        cell.title = symphonyStatsTip(response, cell.getAttribute('data-stats-definition'));
      });
    });
  }).catch(function() {
    if (request !== _symphonyStatsRequest) return;
    _symphonyListStatsRead = null;
    el.querySelectorAll('[data-stats-symphony]').forEach(function(row) {
      if (symphonyRowHidden(row.getAttribute('data-stats-symphony'), choice)) return;
      row.querySelectorAll('[data-stats-measure]').forEach(function(cell) { cell.textContent = 'Statistics unavailable'; });
    });
  });
}


function symphonyStatsActionsHtml(health) {
  var disabled = !health.env_bootstrap_performer_id || health.bootstrap_in_flight ? ' disabled' : '';
  return '<section class="card sym-stats-section"><h3>Actions</h3>' +
    '<button type="button" class="action-btn" id="sym-env-bootstrap-btn"' + disabled + '>Force bootstrap now</button> ' +
    '<span id="sym-env-bootstrap-msg" class="action-msg" role="status"></span> ' +
    '<button type="button" class="action-btn" id="sym-wiki-init-btn">Init wiki</button> ' +
    '<span id="sym-wiki-init-msg" class="action-msg" role="status"></span></section>';
}

function wireSymphonyStatsActions(name) {
  var bootstrap = document.getElementById('sym-env-bootstrap-btn');
  if (bootstrap) bootstrap.onclick = function() {
    bootstrapAction(name, false, bootstrap, document.getElementById('sym-env-bootstrap-msg'));
  };
  var wiki = document.getElementById('sym-wiki-init-btn');
  if (wiki) wiki.onclick = function() {
    symphonyWikiInitAction(name, wiki, document.getElementById('sym-wiki-init-msg'));
  };
}

async function symphonyWikiInitAction(name, button, message) {
  button.disabled = true;
  message.textContent = 'Dispatching...';
  try {
    var response = await fetch('/api/symphonies/' + encodeURIComponent(name) + '/wiki-init',
      {method: 'POST', credentials: 'same-origin'});
    if (response.status === BOOTSTRAP_HTTP_UNAUTHORIZED) message.textContent = 'Sign in to perform this action.';
    else if (response.status === BOOTSTRAP_HTTP_FORBIDDEN) message.textContent = 'You do not have permission to perform this action.';
    else {
      var body = await response.json();
      message.textContent = response.ok ? 'Accepted: building the wiki, a seed PR will open on completion' :
        body.error || body.detail || 'Error ' + response.status;
    }
  } catch (error) {
    message.textContent = 'Network error';
  }
  button.disabled = false;
}
