// 150: the History page. A paginated table of the card's runs, read from the server
// (GET /api/history/runs), with server-side filters and a Checks section for the conductor's
// calls, where identical consecutive checks collapse into one line. Loaded before app.js.
// The pure helpers come first and are checked under node (tests/js/history_checks.js). Every
// value from the server goes through esc(), and a link is an internal path or an https URL.
//
// The filters live in the address bar (h.symphony, h.card, ...), so a reload and Back and
// Forward restore them. The page's cursor stack lives in history.state, keyed by its filters.

var HISTORY_PAGE_LIMIT = 50;
var HISTORY_FILTER_KEYS = ['symphony', 'card', 'stage', 'outcome', 'since', 'until'];
var HISTORY_OUTCOME_LABELS = { completed: 'Completed', failed: 'Failed', unknown: 'Not recorded' };
var HISTORY_NOT_RECORDED = 'not recorded';
var HISTORY_PARAM_PREFIX = 'h.';

// The filters a fresh visit starts with: the header's symphony choice, and nothing else.
function historyDefaultFilters(choice) {
  return { symphony: choice || '', card: '', stage: '', outcome: '', since: '', until: '' };
}

// The filters a History address names. A filter that is absent takes its default, so the
// header's choice still applies to a bare /history. An explicit h.symphony= means All.
function historyFiltersFromSearch(search, choice) {
  var filters = historyDefaultFilters(choice);
  String(search || '').replace(/^\?/, '').split('&').forEach(function(part) {
    var pieces = part.split('=');
    var key = pieces[0];
    if (key.indexOf(HISTORY_PARAM_PREFIX) !== 0) return;
    var name = key.slice(HISTORY_PARAM_PREFIX.length);
    if (HISTORY_FILTER_KEYS.indexOf(name) === -1) return;
    var value;
    try {
      value = decodeURIComponent(pieces.slice(1).join('=').replace(/\+/g, ' '));
    } catch (_malformed) {
      return;
    }
    filters[name] = value;
  });
  return filters;
}

// The address for a set of filters: every parameter that is not a History filter is kept, so the
// header's ?symphony= choice survives. A filter is written when it has a value. The symphony is
// also written when it differs from the header's scope, so an explicit All under a scoped header
// is kept, while a plain /history stays plain.
function historySearchFor(search, filters, scopeSymphony) {
  var keep = [];
  String(search || '').replace(/^\?/, '').split('&').forEach(function(part) {
    if (part && part.split('=')[0].indexOf(HISTORY_PARAM_PREFIX) !== 0) keep.push(part);
  });
  HISTORY_FILTER_KEYS.forEach(function(key) {
    var value = filters[key] || '';
    var written = key === 'symphony' ? value !== (scopeSymphony || '') : !!value;
    if (written) keep.push(HISTORY_PARAM_PREFIX + key + '=' + encodeURIComponent(value));
  });
  return keep.length ? '?' + keep.join('&') : '';
}

// The cursor stack a visit resumes with: the saved one when it was for the same filters, else a
// fresh first page.
function historyStackFor(saved, sig) {
  if (saved && saved.sig === sig && Array.isArray(saved.cursors) && saved.cursors.length &&
      typeof saved.index === 'number' && saved.index >= 0 && saved.index < saved.cursors.length) {
    return { cursors: saved.cursors.slice(), index: saved.index };
  }
  return { cursors: [''], index: 0 };
}

// The query string for one page. Empty filters are left out; the limit is always sent.
function historyQueryString(filters, cursor) {
  var parts = [];
  HISTORY_FILTER_KEYS.forEach(function(key) {
    var value = filters && filters[key];
    if (value) parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
  });
  if (cursor) parts.push('cursor=' + encodeURIComponent(cursor));
  parts.push('limit=' + HISTORY_PAGE_LIMIT);
  return '?' + parts.join('&');
}

// A value the server did not record, with the reason as its tooltip.
function historyNotRecorded(reason) {
  var title = reason ? ' title="' + esc(reason) + '"' : '';
  return '<span class="history-missing"' + title + '>' + HISTORY_NOT_RECORDED + '</span>';
}

// Only an https link is written into the page. Anything else is shown as not recorded.
function historySafeUrl(url) {
  return typeof url === 'string' && /^https:\/\//.test(url) ? url : '';
}

// A row is a conductor check when its stage is one of the conductor's calls.
function historyIsCheck(row) {
  return !!row && row.kind === 'check';
}

// Consecutive checks with the same stage, outcome and decision collapse into one group.
// The rows arrive newest first, so a group's first row is its newest check.
function historyCollapseChecks(rows) {
  var groups = [];
  (Array.isArray(rows) ? rows : []).forEach(function(row) {
    var key = [row.stage || '', row.outcome || '', row.decision || ''].join('|');
    var last = groups.length ? groups[groups.length - 1] : null;
    if (last && last.key === key) {
      last.count += 1;
      last.rows.push(row);
    } else {
      groups.push({ key: key, count: 1, rows: [row] });
    }
  });
  return groups;
}

// The mean duration of a group's checks that have one, or null when none does.
function historyGroupAverage(group) {
  var total = 0;
  var counted = 0;
  group.rows.forEach(function(row) {
    if (typeof row.duration_seconds === 'number') {
      total += row.duration_seconds;
      counted += 1;
    }
  });
  return counted ? total / counted : null;
}

// "12 checks, all completed, avg 5.4s" for a group, or the single check's own figures.
function historyCheckSummary(group) {
  var label = HISTORY_OUTCOME_LABELS[group.rows[0].outcome] || 'Not recorded';
  if (group.count === 1) return label + ', ' + fmtDuration(group.rows[0].duration_seconds);
  var average = historyGroupAverage(group);
  var outcome = group.rows[0].outcome === 'completed' ? 'all completed' : 'all ' + label.toLowerCase();
  return group.count + ' checks, ' + outcome + (average === null ? '' : ', avg ' + fmtDuration(average));
}

// One run as a table row. The transcript link is the viewer's own address (133).
function historyRunRowHtml(row) {
  var outcome = HISTORY_OUTCOME_LABELS[row.outcome] ? row.outcome : 'unknown';
  var title = row.card_title ? '<div class="history-title">' + esc(row.card_title) + '</div>' : '';
  var pr = historySafeUrl(row.pr_url);
  var prHtml = pr
    ? '<a href="' + esc(pr) + '" target="_blank" rel="noopener noreferrer">PR</a>'
    : historyNotRecorded(row.pr_reason);
  var outcomeTitle = row.outcome_text ? ' title="' + esc(row.outcome_text) + '"' : '';
  var stage = row.stage ? esc(row.stage) : historyNotRecorded(row.stage_reason);
  var tokens = typeof row.tokens === 'number' ? usageCount(row.tokens) : historyNotRecorded(row.tokens_reason);
  var cost = typeof row.cost_usd === 'number' ? fmtCost(row.cost_usd) : historyNotRecorded(row.cost_reason);
  var duration = typeof row.duration_seconds === 'number' ? fmtDuration(row.duration_seconds)
    : historyNotRecorded(row.duration_reason);
  var path = runPathFor(row.symphony, row.card, row.run);
  // The card links to this History filtered to the card. The card's pipeline timeline (#147)
  // replaces this link once it ships.
  var cardPath = '/history' + historySearchFor('', {
    symphony: row.symphony, card: row.card, stage: '', outcome: '', since: '', until: '',
  }, '');
  return '<tr>' +
    '<td>' + esc(fmtTime(row.started_at)) + '</td>' +
    '<td>' + esc(row.attributed_symphony || row.symphony || '') + '</td>' +
    '<td><a href="' + esc(cardPath) + '" data-history-card="' + esc(cardPath) + '">' + esc(row.card || '') +
      '</a>' + title + '</td>' +
    '<td>' + stage + '</td>' +
    '<td>' + duration + '</td>' +
    '<td class="history-num">' + tokens + '</td>' +
    '<td class="history-num">' + cost + '</td>' +
    '<td><span class="history-outcome history-outcome-' + esc(outcome) + '"' + outcomeTitle + '>' +
      esc(HISTORY_OUTCOME_LABELS[outcome]) + '</span></td>' +
    '<td>' + prHtml + '</td>' +
    '<td><a href="' + esc(path) + '" data-history-link="' + esc(path) + '">Transcript</a></td>' +
    '</tr>';
}

// The runs table. Empty when nothing matches.
function historyTableHtml(runs) {
  if (!runs.length) return '<p class="empty-state">No runs match these filters.</p>';
  return '<div class="history-table-wrap"><table class="history-table"><thead><tr>' +
    '<th scope="col">Time</th><th scope="col">Symphony</th><th scope="col">Card</th>' +
    '<th scope="col">Stage</th><th scope="col">Duration</th>' +
    '<th scope="col">Tokens</th><th scope="col">Cost</th><th scope="col">Outcome</th>' +
    '<th scope="col">PR</th><th scope="col">Transcript</th>' +
    '</tr></thead><tbody>' + runs.map(historyRunRowHtml).join('') + '</tbody></table></div>';
}

// The conductor's checks, collapsed. Each group is one line with its count and its average.
function historyChecksHtml(checks) {
  if (!checks.length) return '';
  var groups = historyCollapseChecks(checks);
  var lines = groups.map(function(group) {
    var newest = group.rows[0];
    return '<tr><td>' + esc(fmtTime(newest.started_at)) + '</td>' +
      '<td>' + esc(newest.stage || '') + (newest.decision ? ' · ' + esc(newest.decision) : '') + '</td>' +
      '<td>' + esc(historyCheckSummary(group)) + '</td></tr>';
  });
  return '<details class="history-checks"><summary>Checks (' + checks.length + ')</summary>' +
    '<table class="history-table"><thead><tr><th scope="col">Latest</th><th scope="col">Check</th>' +
    '<th scope="col">Result</th></tr></thead><tbody>' + lines.join('') + '</tbody></table></details>';
}

// The filter form. Its values come from the current filters, so a reload keeps what was asked.
function historyFormHtml(filters, names) {
  var symphonies = ['<option value="">All symphonies</option>'].concat((names || []).map(function(name) {
    var selected = filters.symphony === name ? ' selected' : '';
    return '<option value="' + esc(name) + '"' + selected + '>' + esc(name) + '</option>';
  }));
  var outcomes = [['', 'Any outcome']].concat(Object.keys(HISTORY_OUTCOME_LABELS).map(function(key) {
    return [key, HISTORY_OUTCOME_LABELS[key]];
  })).map(function(pair) {
    var selected = filters.outcome === pair[0] ? ' selected' : '';
    return '<option value="' + esc(pair[0]) + '"' + selected + '>' + esc(pair[1]) + '</option>';
  });
  return '<form class="history-filters" data-history-form>' +
    '<label>Symphony <select name="symphony">' + symphonies.join('') + '</select></label>' +
    '<label>Card <input type="text" name="card" value="' + esc(filters.card) + '"></label>' +
    '<label>Stage <input type="text" name="stage" value="' + esc(filters.stage) + '"></label>' +
    '<label>Outcome <select name="outcome">' + outcomes.join('') + '</select></label>' +
    '<label>From <input type="date" name="since" value="' + esc(filters.since) + '"></label>' +
    '<label>To <input type="date" name="until" value="' + esc(filters.until) + '"></label>' +
    '<button type="submit">Apply</button> ' +
    '<button type="button" data-history-action="reset">Clear</button></form>';
}

// The pager: Newer goes back a page this visit has seen, Older asks for the next cursor. Both wait
// while a request is in flight, so a second click cannot move the stack under the first.
function historyPagerHtml(hasNewer, hasOlder, pending) {
  return '<div class="history-pager">' +
    '<button type="button" data-history-action="newer"' + (hasNewer && !pending ? '' : ' disabled') + '>Newer</button> ' +
    '<button type="button" data-history-action="older"' + (hasOlder && !pending ? '' : ' disabled') + '>Older</button> ' +
    '<button type="button" data-history-action="refresh">Refresh</button></div>';
}

// Runs written after the first page loaded. The pages keep to the first page's list, so these
// are counted and offered, not mixed in.
function historyNewerHtml(body) {
  if (!body || !(body.newer_since_anchor > 0)) return '';
  return '<p class="history-notice">New runs since this list loaded: refresh</p>';
}

// Why a listing is incomplete or short, in the server's words, one line per reason that fired.
// Nothing is said when it is whole.
function historyPartialHtml(body) {
  if (!body || !body.partial) return '';
  var details = Array.isArray(body.partial_details) && body.partial_details.length
    ? body.partial_details : ['This listing is incomplete.'];
  return details.map(function(detail) {
    return '<p class="history-notice">' + esc(detail) + '</p>';
  }).join('');
}

// The page for one listing: the filters, the notices, the table, the checks and the pager.
function historyPageHtml(body, filters, names, hasNewer) {
  var rows = Array.isArray(body.rows) ? body.rows : [];
  var runs = rows.filter(function(row) { return !historyIsCheck(row); });
  var checks = rows.filter(historyIsCheck);
  return historyFormHtml(filters, names) + historyNewerHtml(body) + historyPartialHtml(body) +
    historyTableHtml(runs) + historyChecksHtml(checks) + historyPagerHtml(hasNewer, !!body.next_cursor, false);
}

// --- the page ----------------------------------------------------------------- //

// The listing is read again when the scope changes, on a first visit, and once it is this old:
// every snapshot re-renders the page, and a read per snapshot would be a read per second.
var HISTORY_REFRESH_MS = 30000;

var _historyPage = {
  filters: historyDefaultFilters(''), cursors: [''], index: 0, scope: null, seq: 0, loadedAt: 0, body: null,
  pending: false,
};

function historySection() {
  return document.getElementById('history-page-section');
}

function historyMessage(section, text) {
  section.innerHTML = '<p class="empty-state">' + esc(text) + '</p>';
}

// Writes the filters into the address and the cursor stack into history.state. The entry's other
// state (the router's sequence number) is kept.
function historySave() {
  var state = {};
  var previous = history.state || {};
  Object.keys(previous).forEach(function(key) { state[key] = previous[key]; });
  state.historyPage = {
    sig: historyQueryString(_historyPage.filters, ''),
    cursors: _historyPage.cursors.slice(),
    index: _historyPage.index,
  };
  var scopeSymphony = _historyPage.scope === SYMPHONY_ALL ? '' : _historyPage.scope;
  var search = historySearchFor(location.search, _historyPage.filters, scopeSymphony);
  history.replaceState(state, '', location.pathname + search + location.hash);
}

// The page for a scope, called on every snapshot. A new scope (another symphony choice) starts
// the listing over. The filters come from the address on a first visit and on Back and Forward.
function renderHistoryPage(s) {
  var section = historySection();
  if (!section) return;
  var choice = typeof currentSymphonyChoice === 'function' ? currentSymphonyChoice(s) : SYMPHONY_ALL;
  var scopeSymphony = choice === SYMPHONY_ALL ? '' : choice;
  var first = _historyPage.scope === null;
  var changed = !first && _historyPage.scope !== choice;
  var filters = changed ? historyDefaultFilters(scopeSymphony) : historyFiltersFromSearch(location.search, scopeSymphony);
  var sig = historyQueryString(filters, '');
  var saved = history.state && history.state.historyPage;
  var stack = historyStackFor(changed ? null : saved, sig);
  var moved = first || changed || sig !== historyQueryString(_historyPage.filters, '') ||
    stack.index !== _historyPage.index;
  _historyPage.scope = choice;
  _historyPage.filters = filters;
  _historyPage.cursors = stack.cursors;
  _historyPage.index = stack.index;
  historyWire(section);
  if (moved) historySave();
  var stale = Date.now() - _historyPage.loadedAt > HISTORY_REFRESH_MS;
  if (first || changed || stale || moved || !_historyPage.body) loadHistoryRuns();
}

// The section's event handlers, attached once. The page is repainted, but the container stays.
function historyWire(section) {
  if (section.getAttribute('data-history-wired') === '1') return;
  section.setAttribute('data-history-wired', '1');
  section.addEventListener('submit', function(event) {
    if (!event.target || !event.target.hasAttribute('data-history-form')) return;
    event.preventDefault();
    var data = new FormData(event.target);
    var filters = historyDefaultFilters('');
    HISTORY_FILTER_KEYS.forEach(function(key) { filters[key] = String(data.get(key) || '').trim(); });
    _historyPage.filters = filters;
    _historyPage.cursors = [''];
    _historyPage.index = 0;
    historySave();
    loadHistoryRuns();
  });
  section.addEventListener('click', function(event) {
    var link = event.target && event.target.closest
      ? event.target.closest('[data-history-link], [data-history-card]') : null;
    if (link) {
      navigate(event, link.getAttribute('data-history-link') || link.getAttribute('data-history-card'));
      return;
    }
    var button = event.target && event.target.closest ? event.target.closest('[data-history-action]') : null;
    if (!button || button.disabled) return;
    historyAction(button.getAttribute('data-history-action'));
  });
}

// Older and Newer are ignored while a page is loading. Reset, Refresh and a new filter supersede
// the request in flight, since the sequence number drops its answer.
function historyAction(action) {
  if ((action === 'older' || action === 'newer') && _historyPage.pending) return;
  if (action === 'reset') {
    _historyPage.filters = historyDefaultFilters(_historyPage.scope === SYMPHONY_ALL ? '' : _historyPage.scope);
    _historyPage.cursors = [''];
    _historyPage.index = 0;
    historySave();
  } else if (action === 'refresh') {
    // A refresh is page one again. Keeping the cursor would keep the anchor it carries, so the
    // notice of runs written since the list loaded could never clear.
    _historyPage.cursors = [''];
    _historyPage.index = 0;
    historySave();
  } else if (action === 'newer' && _historyPage.index > 0) {
    _historyPage.index -= 1;
    historySave();
  } else if (action === 'older' && _historyPage.body && _historyPage.body.next_cursor) {
    _historyPage.cursors = _historyPage.cursors.slice(0, _historyPage.index + 1);
    _historyPage.cursors.push(_historyPage.body.next_cursor);
    _historyPage.index += 1;
    historySave();
  }
  loadHistoryRuns();
}

// Disables the pager's Older and Newer buttons while a request is in flight. The page repaints
// them when the answer arrives, so only the disabling is needed here.
function historyMarkPending(section, pending) {
  if (!section || typeof section.querySelectorAll !== 'function') return;
  section.querySelectorAll('[data-history-action="older"], [data-history-action="newer"]').forEach(function(button) {
    button.disabled = pending;
  });
}

function loadHistoryRuns() {
  var section = historySection();
  if (!section) return;
  var seq = _historyPage.seq + 1;
  _historyPage.seq = seq;
  _historyPage.loadedAt = Date.now();
  _historyPage.pending = true;
  historyMarkPending(section, true);
  var cursor = _historyPage.cursors[_historyPage.index] || '';
  fetch('/api/history/runs' + historyQueryString(_historyPage.filters, cursor), { credentials: 'same-origin' })
    .then(function(response) {
      return response.json().then(function(body) { return { status: response.status, body: body }; });
    })
    .then(function(result) {
      if (seq !== _historyPage.seq) return;
      historyShow(section, result);
    })
    .catch(function() {
      if (seq !== _historyPage.seq) return;
      _historyPage.pending = false;
      historyMessage(section, 'The run list could not be loaded.');
    });
}

// Paints one response. The status and the server's message come first, so a 400 or a 404 says
// why; a 200 that has no listing says what the daemon reports.
function historyShow(section, result) {
  _historyPage.pending = false;
  var body = result.body || {};
  if (result.status !== 200) {
    historyMessage(section, body.detail || 'The run list could not be loaded.');
    return;
  }
  if (!body.available) {
    historyMessage(section, body.reason || 'Run history is not available on this daemon.');
    return;
  }
  _historyPage.body = body;
  section.innerHTML = historyPageHtml(body, _historyPage.filters, historySymphonyNames(), _historyPage.index > 0);
}

// The symphony choices the filter offers: the names the header's scope shows.
function historySymphonyNames() {
  var scoped = typeof currentScopedState === 'function' ? currentScopedState() : null;
  return typeof symphonyNames === 'function' ? symphonyNames(scoped) : [];
}
