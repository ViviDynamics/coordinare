// Bootstrap run detail and operator actions. Live data uses the existing SSE connection.
var _bootstrapDetail = null;
var _bootstrapRequest = 0;
var _bootstrapActivity = [];
var _bootstrapActivityRun = '';
var _bootstrapLastAction = null;
var BOOTSTRAP_EVENT_LIMIT = 500;
var BOOTSTRAP_JSON_INDENT = 2;
var BOOTSTRAP_IDENTITY_TEXT_CHARS = 200;
var BOOTSTRAP_IDENTITY_SUMMARY_CHARS = 80;
var BOOTSTRAP_HTTP_UNAUTHORIZED = 401;
var BOOTSTRAP_HTTP_FORBIDDEN = 403;

function bootstrapEventEntry(event, run) {
  return event.activity_type ? event : afEntryFromBackend(event, run);
}

function bootstrapEventRow(event, run) {
  var original = bootstrapEventEntry(event, run.run_id);
  var entry = {};
  Object.keys(original).forEach(function(key) { entry[key] = original[key]; });
  entry.session_id = run.job_id || '';
  return afEventRowHtml(entry);
}

function bootstrapMergeActivity(run, events) {
  if (_bootstrapActivityRun !== run) { _bootstrapActivity = []; _bootstrapActivityRun = run; }
  var seen = {};
  _bootstrapActivity = events.map(function(event) { return bootstrapEventEntry(event, run); })
    .concat(_bootstrapActivity).filter(function(entry) {
      var identity = JSON.stringify([entry.session_id, entry.activity_type,
        entry.event_at || entry.timestamp, entry.transcript_seq, entry.status,
        String(entry.text || '').slice(0, BOOTSTRAP_IDENTITY_TEXT_CHARS),
        String(entry.summary || '').slice(0, BOOTSTRAP_IDENTITY_SUMMARY_CHARS)]);
      if (Object.prototype.hasOwnProperty.call(seen, identity)) return false;
      seen[identity] = true;
      return true;
    }).sort(function(a, b) {
      return String(a.event_at || a.timestamp).localeCompare(String(b.event_at || b.timestamp));
    }).slice(-BOOTSTRAP_EVENT_LIMIT);
  return _bootstrapActivity;
}

function bootstrapRunPath(name, run) {
  return '/symphonies/' + encodeURIComponent(name) + '/bootstrap/' + encodeURIComponent(run);
}

function isBootstrapRunPath(path) {
  return /^\/symphonies\/[^/]+\/bootstrap\/[^/]+$/.test(path);
}

function bootstrapAddress() {
  if (!isBootstrapRunPath(location.pathname)) return null;
  var parts = location.pathname.split('/');
  try { return {name: decodeURIComponent(parts[2]), run: decodeURIComponent(parts[4])}; }
  catch (e) { return null; }
}

function bootstrapScopeAllows(name) {
  var s = currentScopedState();
  if (!s || typeof currentSymphonyChoice !== 'function') return true;
  var choice = currentSymphonyChoice(s);
  return !choice || choice === name;
}

function bootstrapRunHtml(run) {
  var rows = [
    ['State', run.state === 'stop_unconfirmed' ? 'Stop unconfirmed' : run.state || run.outcome], ['Backend', run.backend], ['Model', run.model],
    ['Pod', run.pod], ['Performer', run.performer_id], ['Job', run.job_id],
    ['Commit', run.commit], ['Start', run.started_at], ['Elapsed seconds', run.duration_seconds],
    ['Last activity', run.last_activity_at]
  ];
  var manifest = Array.isArray(run.manifest) ? run.manifest : [];
  var tail = Array.isArray(run.reason_tail) ? run.reason_tail : [];
  var events = Array.isArray(run.events) ? run.events : [];
  return '<p><a href="' + esc('/symphonies/' + encodeURIComponent(run.symphony)) +
    '" onclick="navigate(event,this.pathname)">Symphony</a></p><h2>Bootstrap run: ' + esc(run.symphony) +
    '</h2><p class="muted">' + esc(run.run_id) + '</p><div class="runs-facts">' + rows.map(function(row) {
    return '<div class="run-fact"><span class="run-fact-label">' + esc(row[0]) + '</span><span>' +
      esc(row[1] == null ? 'Not recorded' : String(row[1])) + '</span></div>';
  }).join('') + '</div>' +
    (run.job_id ? '<p><a id="bootstrap-transcript" href="' + esc('/runs/' + encodeURIComponent(run.job_id)) +
      '" onclick="navigate(event,this.pathname)">Transcript</a></p>' : '<p>Transcript not available</p>') +
    '<button class="action-btn" id="bootstrap-retry" data-sym="' + esc(run.symphony) + '">Retry now</button> ' +
    (run.running || run.outcome === 'running' ? '<button class="action-btn" id="bootstrap-cancel" data-sym="' +
      esc(run.symphony) + '">Cancel</button>' : '') +
    '<h3>Environment manifest</h3><pre class="run-prose">' + esc(JSON.stringify(manifest, null, BOOTSTRAP_JSON_INDENT)) + '</pre>' +
    '<h3>Verify verdict</h3><p>' + esc(run.verify_verdict || 'Not run') + '</p><pre class="run-prose">' + esc(tail.join('\n')) + '</pre>' +
    '<details><summary>History entry</summary><pre class="run-prose">' +
    esc(JSON.stringify(run.history || {}, null, BOOTSTRAP_JSON_INDENT)) + '</pre></details>' +
    '<h3>Activity</h3><p class="muted">Most recent retained events. Open the transcript for the full run.</p>' +
    '<div id="bootstrap-run-events" class="runs-scroll" tabindex="0" aria-label="Bootstrap activity">' + events.map(function(event) {
      return bootstrapEventRow(event, run);
    }).join('') + '</div>';
}

function bootstrapActionMessage(body) {
  return String(body.error || body.detail || body.status || 'Action completed') +
    (body.next_reset_at ? ' Next allowed time: ' + String(body.next_reset_at) : '') +
    (body.stopped === false ? ' The runtime could not confirm that the job stopped.' : '');
}

function bootstrapListMessage() {
  return _bootstrapLastAction && bootstrapScopeAllows(_bootstrapLastAction.name) ?
    _bootstrapLastAction.name + ': ' + _bootstrapLastAction.text : '';
}

function bootstrapAction(name, cancel, button, message) {
  var effect = cancel ? 'Cancel this bootstrap? The retry budget will stay unchanged.' :
    'Reset the bootstrap retry budget and request a new run? Manual retries have a 15 minute cooldown.';
  if (!window.confirm(effect)) return;
  button.disabled = true;
  fetch('/api/symphonies/' + encodeURIComponent(name) + '/env-bootstrap' + (cancel ? '/cancel' : ''),
    {method: 'POST', credentials: 'same-origin'}).then(function(response) {
    if (response.status === BOOTSTRAP_HTTP_UNAUTHORIZED) {
      return {error: 'Sign in to perform this action.'};
    }
    if (response.status === BOOTSTRAP_HTTP_FORBIDDEN) {
      return {error: 'You do not have permission to perform this action.'};
    }
    return response.json().then(function(body) {
      if (!response.ok && !body.error && !body.detail) body.error = 'Action failed: HTTP ' + String(response.status);
      return body;
    }, function() {
      return {error: response.ok ? 'The server returned an invalid response.' : 'Action failed: HTTP ' + String(response.status)};
    });
  }).then(function(body) {
    var result = bootstrapActionMessage(body);
    if (message) message.textContent = result;
    if (message && message.id === 'sym-list-msg') _bootstrapLastAction = {name: name, text: result};
    if (isBootstrapRunPath(location.pathname)) bootstrapPageRefresh();
  }).catch(function() {
    if (message) message.textContent = 'Network error';
  }).then(function() { button.disabled = false; });
}

function bootstrapPageRefresh() {
  var address = bootstrapAddress();
  var detail = document.getElementById('bootstrap-run-detail');
  if (!address || !detail) return;
  var request = ++_bootstrapRequest;
  if (!bootstrapScopeAllows(address.name)) {
    _bootstrapDetail = null;
    _bootstrapActivity = [];
    var message = document.getElementById('bootstrap-action-msg');
    if (message) message.textContent = '';
    detail.textContent = 'Hidden: another symphony is shown';
    return;
  }
  fetch('/api/symphonies/' + encodeURIComponent(address.name) + '/bootstrap-runs/' + encodeURIComponent(address.run),
    {credentials: 'same-origin'}).then(function(response) {
    return response.json();
  }).then(function(body) {
    if (request !== _bootstrapRequest || !bootstrapAddress() || !bootstrapScopeAllows(address.name)) return;
    if (body.error) { detail.textContent = body.error; return; }
    body.events = bootstrapMergeActivity(body.run_id, body.events || []);
    _bootstrapDetail = body;
    detail.innerHTML = bootstrapRunHtml(body);
    ['retry', 'cancel'].forEach(function(kind) {
      var button = document.getElementById('bootstrap-' + kind);
      if (button) button.onclick = function() {
        bootstrapAction(address.name, kind === 'cancel', button, document.getElementById('bootstrap-action-msg'));
      };
    });
  }).catch(function() {
    if (request === _bootstrapRequest) detail.textContent = 'Bootstrap run could not be loaded';
  });
}

function bootstrapLiveActivity(entries) {
  var address = bootstrapAddress();
  var feed = document.getElementById('bootstrap-run-events');
  if (!address || !bootstrapScopeAllows(address.name)) return;
  var mine = (entries || []).filter(function(entry) {
    return entry.session_id === address.run && entry.symphony === address.name;
  });
  var merged = bootstrapMergeActivity(address.run, mine);
  if (!feed || !_bootstrapDetail || _bootstrapDetail.run_id !== address.run) return;
  feed.innerHTML = merged.map(function(event) { return bootstrapEventRow(event, _bootstrapDetail); }).join('');
}
