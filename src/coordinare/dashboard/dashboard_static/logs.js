// 271: persistent incident history. The server owns filtering, redaction and paging.
var LOGS_PAGE_LIMIT = 50;
var LOGS_FILTERS = ['start', 'end', 'symphony', 'card_id', 'run_id', 'stage', 'source', 'kind', 'level', 'text'];
var LOGS_LABELS = {start: 'From', end: 'To', symphony: 'Symphony', card_id: 'Card', run_id: 'Run',
  stage: 'Stage', source: 'Source', kind: 'Kind', level: 'Level', text: 'Search'};
var LOGS_JSON_INDENT = 2;
var LOGS_MAX_SAFE_INTEGER = 9007199254740991;
var _logsPage = {scope: null, filters: {}, cursors: [''], index: 0, next: null,
  live: false, busy: false, dirty: false, seq: 0};

function logsQueryString(filters, cursor) {
  var parts = ['limit=' + LOGS_PAGE_LIMIT];
  LOGS_FILTERS.forEach(function(key) {
    if (filters[key]) parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(filters[key]));
  });
  if (cursor) parts.push('cursor=' + encodeURIComponent(cursor));
  return '?' + parts.join('&');
}

function logsLink(path, label) {
  return '<a href="' + esc(path) + '" data-logs-link="' + esc(path) + '">' + esc(label) + '</a>';
}

function logsRowHtml(row) {
  var address = row.symphony && row.card_id;
  var card = address ? logsLink(cardPathFor(row.symphony, row.card_id), row.card_id) : esc(row.card_id || '');
  var run = row.run_id ? (address ? runPathFor(row.symphony, row.card_id, row.run_id) :
    '/runs/' + encodeURIComponent(row.run_id)) : '';
  var links = run ? logsLink(run, 'Run') : '';
  var seq = (row.data || {}).transcript_seq;
  if (run && typeof seq === 'number' && seq >= 0 && seq <= LOGS_MAX_SAFE_INTEGER && Math.floor(seq) === seq) {
    links += ' ' + logsLink(run + runEventHash(seq), 'Record #' + String(seq));
  }
  return '<tr data-event-id="' + esc(String(row.id)) + '"><td>' + timeHtml(row.ts, 'unknown') +
    '</td><td>' + esc(row.level || '') + '</td><td>' + esc(row.source || '') + '</td><td>' +
    esc(row.symphony || '') + '</td><td>' + card + '</td><td>' + esc(row.kind || '') +
    '</td><td><div>' + esc(row.message || '') + '</div>' + links +
    '<details><summary>Data</summary><pre class="logs-data">' +
    esc(JSON.stringify(row.data || {}, null, LOGS_JSON_INDENT)) + '</pre></details></td></tr>';
}

function logsFormHtml() {
  var filters = _logsPage.filters;
  return '<form class="history-filters" data-logs-form>' + LOGS_FILTERS.map(function(key) {
    var isTime = key === 'start' || key === 'end';
    var value = filters[key] || '';
    if (isTime && value) value = value.slice(0, 'YYYY-MM-DDTHH:mm'.length);
    return '<label>' + esc(LOGS_LABELS[key]) + ' <input name="' + esc(key) + '" type="' +
      esc(isTime ? 'datetime-local' : 'text') + '" value="' + esc(value) + '"' +
      (key === 'symphony' && _logsPage.scope ? ' disabled' : '') + '></label>';
  }).join('') + '<span>Times in UTC</span><button type="submit">Apply</button>' +
    '<button type="button" data-logs-action="clear">Clear</button>' +
    '<label><input id="logs-live" type="checkbox"' + (_logsPage.live ? ' checked' : '') + '> Live</label></form>' +
    '<div id="logs-results" role="status">Loading...</div>';
}

function logsPaint(body) {
  var target = document.getElementById('logs-results');
  if (!target) return;
  var rows = body.events || [];
  target.innerHTML = (rows.length ? '<div class="history-table-wrap"><table class="history-table">' +
    '<thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Symphony</th><th>Card</th><th>Kind</th><th>Message</th></tr></thead>' +
    '<tbody id="logs-rows">' + rows.map(logsRowHtml).join('') + '</tbody></table></div>' : '<p>No events match these filters.</p>') +
    '<div class="history-pager"><button type="button" data-logs-action="newer"' +
    (_logsPage.index > 0 ? '' : ' disabled') + '>Newer</button>' +
    '<button type="button" data-logs-action="older"' + (body.cursor ? '' : ' disabled') + '>Older</button>' +
    '<button type="button" data-logs-action="refresh">Refresh</button></div>';
}

function logsFirstPage() {
  _logsPage.cursors = [''];
  _logsPage.index = 0;
  loadLogs();
}

function renderLogsPage(s, force) {
  var section = document.getElementById('logs-page-section');
  if (!section || !s) return;
  var choice = currentSymphonyChoice(s) || '';
  if (_logsPage.scope === choice) {
    if (force) loadLogs();
    return;
  }
  _logsPage.scope = choice;
  _logsPage.filters = {symphony: choice};
  section.innerHTML = logsFormHtml();
  logsWire(section);
  logsFirstPage();
}

function logsWire(section) {
  if (section.getAttribute('data-logs-wired') === '1') return;
  section.setAttribute('data-logs-wired', '1');
  section.addEventListener('submit', function(event) {
    if (!event.target.hasAttribute('data-logs-form')) return;
    event.preventDefault();
    var data = new FormData(event.target);
    var filters = {};
    LOGS_FILTERS.forEach(function(key) {
      var value = String(data.get(key) || '').trim();
      if ((key === 'start' || key === 'end') && value) value += ':00Z';
      filters[key] = value;
    });
    if (_logsPage.scope) filters.symphony = _logsPage.scope;
    _logsPage.filters = filters;
    logsFirstPage();
  });
  section.addEventListener('change', function(event) {
    if (event.target.id !== 'logs-live') return;
    _logsPage.live = event.target.checked;
    if (_logsPage.live) logsFirstPage();
  });
  section.addEventListener('click', function(event) {
    var link = event.target.closest('[data-logs-link]');
    if (link) { navigate(event, link.getAttribute('data-logs-link')); return; }
    var button = event.target.closest('[data-logs-action]');
    if (!button || button.disabled) return;
    logsAction(button.getAttribute('data-logs-action'));
  });
}

function logsAction(action) {
  if ((action === 'older' || action === 'newer') && _logsPage.busy) return;
  if (action === 'clear') {
    _logsPage.filters = {symphony: _logsPage.scope || ''};
    document.getElementById('logs-page-section').innerHTML = logsFormHtml();
    logsFirstPage();
    return;
  }
  if (action === 'refresh') { logsFirstPage(); return; }
  _logsPage.live = false;
  document.getElementById('logs-live').checked = false;
  if (action === 'older' && _logsPage.next) {
    _logsPage.cursors = _logsPage.cursors.slice(0, _logsPage.index + 1);
    _logsPage.cursors.push(_logsPage.next);
    _logsPage.index += 1;
  } else if (action === 'newer' && _logsPage.index > 0) {
    _logsPage.index -= 1;
  }
  loadLogs();
}

// Coalesce commit bursts, retaining one catchup when a commit arrives during a read.
function logsChanged() {
  if (location.pathname !== '/logs' || !_logsPage.live || _logsPage.scope === null) return;
  if (_logsPage.busy) { _logsPage.dirty = true; return; }
  logsFirstPage();
}

function loadLogs() {
  var seq = ++_logsPage.seq;
  _logsPage.busy = true;
  _logsPage.dirty = false;
  return fetch('/api/events' + logsQueryString(_logsPage.filters, _logsPage.cursors[_logsPage.index]),
    {credentials: 'same-origin'}).then(function(response) {
    if (!response.ok) throw new Error('Event history unavailable (' + response.status + ').');
    return response.json();
  }).then(function(body) {
    if (seq !== _logsPage.seq) return;
    _logsPage.next = body.cursor;
    logsPaint(body);
  }).catch(function(error) {
    if (seq !== _logsPage.seq) return;
    var target = document.getElementById('logs-results');
    if (target) target.innerHTML = '<p>' + esc(error.message) + '</p><button data-logs-action="refresh">Retry</button>';
  }).then(function() {
    if (seq !== _logsPage.seq) return;
    _logsPage.busy = false;
    if (_logsPage.dirty) logsChanged();
  });
}
