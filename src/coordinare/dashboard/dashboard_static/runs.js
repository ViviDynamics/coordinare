// 133: the run transcript viewer, at /runs/<symphony>/<card>/<run> (or /runs/<run>).
// Loaded before app.js. The pure helpers come first and are checked under node
// (tests/js/runs_checks.js). Every value from a transcript goes through esc() or
// textToHtml(), so it is shown as text. A link is only an internal path or an
// http(s) URL, and nothing from a transcript is used as a script, a style or markup.

var RUN_PAGE_LIMIT = 200;
// The most records one open run keeps in the page. Beyond it the viewer says so and
// points at the JSONL download, so a long run is never cut without a notice.
var RUN_MAX_LOADED = 20000;
var RUN_ROW_ESTIMATE = 56;
var RUN_WINDOW_MARGIN = 8;
var RUN_TITLE_CHARS = 90;
var RUN_CARD_CACHE_MS = 30000;
var RUN_SEARCH_LIMIT = 50;
var RUN_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
var RUN_KIND_LABELS = {
  user: 'User', assistant_text: 'Assistant', thinking: 'Thinking', tool_call: 'Tool call',
  tool_result: 'Tool result', usage: 'Tokens', result: 'Result', error: 'Error',
  system: 'System', capability: 'Capability', truncated: 'Truncated', unreadable: 'Unreadable',
  run_header: 'Header'
};
var RUN_TYPE_FILTERS = [
  'user', 'assistant_text', 'thinking', 'tool_call', 'tool_result', 'usage', 'result',
  'error', 'system', 'capability', 'truncated'
];
var RUN_FULL_FIELDS = ['text', 'input', 'output'];

// --- pure helpers ----------------------------------------------------------- //

function isRunsPath(path) {
  return typeof path === 'string' && path.indexOf('/runs/') === 0;
}

// The address a /runs path names: {kind:'full', symphony, card, run}, {kind:'short', run},
// or {invalid:true}. A segment that does not decode, or is not a plausible name, is invalid.
// The server checks the address again, so this only decides what to ask for.
function runAddressFromPath(path) {
  if (!isRunsPath(path)) return { invalid: true };
  var parts = path.slice('/runs/'.length).replace(/\/+$/, '').split('/');
  var decoded = [];
  for (var i = 0; i < parts.length; i++) {
    var value;
    try { value = decodeURIComponent(parts[i]); } catch (_malformed) { return { invalid: true }; }
    if (!RUN_NAME_PATTERN.test(value)) return { invalid: true };
    decoded.push(value);
  }
  if (decoded.length === 1) return { kind: 'short', run: decoded[0] };
  if (decoded.length === 3) return { kind: 'full', symphony: decoded[0], card: decoded[1], run: decoded[2] };
  return { invalid: true };
}

function runPathFor(symphony, card, run) {
  return '/runs/' + [symphony, card, run].map(encodeURIComponent).join('/');
}

function runApiBase(addr) {
  return '/api/runs/' + [addr.symphony, addr.card, addr.run].map(encodeURIComponent).join('/');
}

function runEventHash(seq) {
  return '#e' + String(seq);
}

// The seq a #e<seq> fragment names, or null for any other fragment.
function runSeqFromHash(hash) {
  var match = /^#e(\d+)$/.exec(hash || '');
  return match ? parseInt(match[1], 10) : null;
}

// The value of one query parameter, decoded, or '' when it is absent.
function runQueryParam(search, name) {
  var pattern = new RegExp('[?&]' + name + '=([^&]*)');
  var match = pattern.exec(search || '');
  if (!match) return '';
  try { return decodeURIComponent(match[1].replace(/\+/g, ' ')); } catch (_malformed) { return ''; }
}

// A link target from a transcript or a record: http(s) only. Anything else is dropped.
function runsSafeHref(url) {
  if (typeof url !== 'string') return '';
  return /^https?:\/\//i.test(url) ? url : '';
}

// Plain text with its line breaks kept. Escaped first, so no markup survives.
function textToHtml(text) {
  var value = text == null ? '' : String(text);
  return esc(value).replace(/\r\n?|\n/g, '<br>');
}

function fmtRunSeconds(seconds) {
  if (seconds == null || isNaN(seconds)) return '-';
  if (seconds < 60) return seconds.toFixed(1) + 's';
  var minutes = Math.floor(seconds / 60);
  var rest = Math.round(seconds - minutes * 60);
  return minutes + 'm ' + rest + 's';
}

function runSecondsBetween(start, end) {
  if (!start || !end) return null;
  var a = Date.parse(String(start).replace(/(\.\d{3})\d+/, '$1'));
  var b = Date.parse(String(end).replace(/(\.\d{3})\d+/, '$1'));
  if (isNaN(a) || isNaN(b) || b < a) return null;
  return (b - a) / 1000;
}

// "Bash · pytest -q tests/ · exit 1 · 2.3s". The result is optional: a call whose
// result is not loaded yet shows only its name and argument.
function toolRowText(call, result) {
  var parts = [call.tool_name || 'tool'];
  if (call.arg) parts.push(call.arg);
  if (result) {
    if (result.exit_code != null) parts.push('exit ' + result.exit_code);
    else if (result.is_error) parts.push('error');
    else parts.push('ok');
    var seconds = runSecondsBetween(call.ts, result.ts);
    if (seconds != null) parts.push(fmtRunSeconds(seconds));
  }
  return parts.join(' · ');
}

function resultRowText(result, call) {
  var parts = ['result'];
  if (result.exit_code != null) parts.push('exit ' + result.exit_code);
  else if (result.is_error) parts.push('error');
  else parts.push('ok');
  if (call) {
    var seconds = runSecondsBetween(call.ts, result.ts);
    if (seconds != null) parts.push(fmtRunSeconds(seconds));
  }
  return parts.join(' · ');
}

function recordIsError(rec) {
  return rec.type === 'error' || rec.is_error === true || rec.unreadable === true;
}

// The client-side filter over loaded records. It agrees with the server: errors narrow
// to failures, types narrow to the named types, and an unreadable line is shown unless
// a type filter is set without the errors filter.
function filterRecords(records, filter) {
  var types = filter && filter.types ? filter.types : [];
  var errors = !!(filter && filter.errors);
  return (records || []).filter(function(rec) {
    if (errors && !recordIsError(rec)) return false;
    if (rec.unreadable) return errors || types.length === 0;
    return types.length === 0 || types.indexOf(rec.type) !== -1;
  });
}

function recordKind(rec) {
  if (rec.unreadable) return 'unreadable';
  if (rec.type === 'tool_call' || rec.type === 'tool_result') return 'tool';
  if (recordIsError(rec)) return 'error';
  return rec.type || 'event';
}

function recordKindLabel(rec) {
  if (rec.unreadable) return RUN_KIND_LABELS.unreadable;
  return RUN_KIND_LABELS[rec.type] || (rec.type || 'Event');
}

// The one-line title of a record. It is text, escaped by the caller.
function recordTitle(rec) {
  if (rec.unreadable) return 'line ' + rec.line + ' could not be read';
  if (rec.type === 'tool_call') return [rec.tool_name || 'tool', rec.arg || ''].filter(Boolean).join(' · ');
  if (rec.type === 'usage') return usageLine(rec.usage);
  var first = String(rec.text || rec.preview || '').split(/\r?\n/)[0] || '';
  return first.length > RUN_TITLE_CHARS ? first.slice(0, RUN_TITLE_CHARS) + '...' : first;
}

// "in 100 · out 20 · cache 5 read, 2 write" from one usage object.
function usageLine(usage) {
  if (!usage || typeof usage !== 'object') return 'no usage';
  var parts = [];
  if (usage.input_tokens != null) parts.push('in ' + usage.input_tokens);
  if (usage.output_tokens != null) parts.push('out ' + usage.output_tokens);
  var cache = [];
  if (usage.cache_read != null) cache.push(usage.cache_read + ' read');
  if (usage.cache_write != null) cache.push(usage.cache_write + ' write');
  if (cache.length) parts.push('cache ' + cache.join(', '));
  return parts.length ? parts.join(' · ') : 'no token counts';
}

// Where a virtualised list shows rows. heights[i] is row i's height; the window is the rows
// that cover the viewport, widened by margin rows each side, with the space above and below
// them as padding so the scrollbar keeps its real length.
function windowFor(heights, scrollTop, viewport, margin) {
  var count = heights.length;
  var top = 0;
  var first = 0;
  while (first < count && top + heights[first] <= scrollTop) {
    top += heights[first];
    first += 1;
  }
  var start = Math.max(0, first - margin);
  var padTop = 0;
  for (var s = 0; s < start; s++) padTop += heights[s];
  var end = first;
  var edge = scrollTop + viewport;
  var bottom = top;
  while (end < count && bottom < edge) {
    bottom += heights[end];
    end += 1;
  }
  end = Math.min(count, end + margin);
  var padBottom = 0;
  for (var e = end; e < count; e++) padBottom += heights[e];
  return { start: start, end: end, padTop: padTop, padBottom: padBottom };
}

// Runs grouped by stage, in the order the stages first appear (newest run first within each).
function groupRunsByStage(runs) {
  var order = [];
  var groups = {};
  (runs || []).forEach(function(run) {
    var stage = run.stage || 'unknown stage';
    if (!groups[stage]) {
      groups[stage] = [];
      order.push(stage);
    }
    groups[stage].push(run);
  });
  return order.map(function(stage) { return { stage: stage, runs: groups[stage] }; });
}

function runCopyLinkUrl(origin, symphony, card, run, seq) {
  return origin + runPathFor(symphony, card, run) + runEventHash(seq);
}

function personaHistoryPath(role, version) {
  if (typeof version !== 'number' || version !== Math.floor(version)) return '';
  return '/api/personas/' + encodeURIComponent(role) + '/history/' + String(version);
}

// The activity entry's link into its run. It opens the run at the entry's time. 135: the label
// is optional, so a cut line can say "Open full transcript" while a row says "Transcript".
function runEntryLinkHtml(entry, label) {
  // Self-contained (the pattern is inline), so the activity feed can carry this function alone.
  var session = entry && entry.session_id ? String(entry.session_id) : '';
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(session)) return '';
  // The performer's own event time when the entry has one: the transcript's record times are
  // on the same clock. The daemon's observation time (entry.timestamp) lags behind it.
  var when = entry.event_at || entry.timestamp || '';
  var href = '/runs/' + encodeURIComponent(session) +
    (when ? '?at=' + encodeURIComponent(String(when)) : '');
  return '<a class="run-entry-link" href="' + esc(href) + '" ' +
    'onclick="event.stopPropagation();navigate(event,this.getAttribute(\'href\'))" ' +
    'onkeydown="event.stopPropagation()">' + esc(label || 'Transcript') + '</a>';
}

// The #146 run detail's link to a session's transcript (the run id is the session id).
function runSessionLinkHtml(runId) {
  var run = String(runId || '');
  if (!RUN_NAME_PATTERN.test(run)) return '';
  var href = '/runs/' + encodeURIComponent(run);
  return '<a class="run-entry-link" href="' + esc(href) + '" onclick="navigate(event,this.getAttribute(\'href\'))">Transcript</a>';
}

// --- rendering --------------------------------------------------------------- //

function runMoreHtml(rec, field) {
  if (!rec[field + '_truncated']) return '';
  return '<button type="button" class="run-more" data-action="full" data-line="' + esc(String(rec.line)) +
    '" data-field="' + esc(field) + '">Load full ' + esc(field) + ' (' + esc(String(rec[field + '_bytes'] || 0)) +
    ' bytes)</button>';
}

function runPreviewHtml(rec, field) {
  var value = rec[field];
  if (value == null) return '';
  var body = rec[field + '_format'] === 'json' ? '<pre class="run-pre">' + esc(value) + '</pre>' : '<div class="run-prose">' + textToHtml(value) + '</div>';
  return body + runMoreHtml(rec, field);
}

function runRowHtml(rec, ctx) {
  var key = String(rec.line);
  var open = ctx.open[key] ? ' open' : '';
  var seqLabel = rec.unreadable ? 'line ' + rec.line : '#' + (rec.seq == null ? '' : rec.seq);
  var head = '<div class="run-row-head"><span class="run-row-seq">' + esc(seqLabel) + '</span> ' +
    '<span class="run-kind run-kind-' + esc(recordKind(rec)) + '">' + esc(recordKindLabel(rec)) + '</span> ' +
    '<span class="run-row-title">' + esc(recordTitle(rec)) + '</span>' +
    (rec.seq != null ? ' <button type="button" class="run-copy" data-action="copy" data-seq="' + esc(String(rec.seq)) + '">Copy link</button>' : '') +
    '</div>';
  var body = '';
  if (rec.unreadable) {
    body = '<div class="run-unreadable"><p>This line is not a JSON object (' + esc(String(rec.bytes)) + ' bytes). It is kept as it was written.</p>' +
      '<pre class="run-pre">' + esc(rec.preview || '') + '</pre></div>';
  } else if (rec.type === 'thinking') {
    body = '<details class="run-think"' + open + ' data-line="' + esc(key) + '"><summary>Thinking</summary>' + runPreviewHtml(rec, 'text') + '</details>';
  } else if (rec.type === 'tool_call') {
    var pair = ctx.results[rec.call_id] || null;
    body = '<details class="run-tool"' + open + ' data-line="' + esc(key) + '"><summary>' + esc(toolRowText(rec, pair)) + '</summary>' +
      '<div class="run-io"><div class="run-io-label">Input</div>' + runPreviewHtml(rec, 'input') + '</div></details>';
  } else if (rec.type === 'tool_result') {
    var call = ctx.calls[rec.call_id] || null;
    body = '<details class="run-tool run-result"' + open + ' data-line="' + esc(key) + '"><summary>' + esc(resultRowText(rec, call)) + '</summary>' +
      '<div class="run-io"><div class="run-io-label">Output</div>' + runPreviewHtml(rec, 'output') + '</div></details>';
  } else if (rec.type === 'usage') {
    body = '<div class="run-usage">' + esc(usageLine(rec.usage)) + '</div>';
  } else if (rec.type === 'error') {
    body = '<div class="run-error">' + runPreviewHtml(rec, 'text') + '</div>';
  } else if (rec.type === 'assistant_text' || rec.type === 'user') {
    body = runPreviewHtml(rec, 'text');
  } else {
    body = runPreviewHtml(rec, 'text') + (rec.output != null ? runPreviewHtml(rec, 'output') : '');
  }
  var anchor = rec.seq != null ? 'e' + rec.seq : 'l' + rec.line;
  return '<div class="run-row run-row-' + esc(recordKind(rec)) + '" id="' + esc(anchor) + '" data-line="' + esc(key) + '" role="listitem">' +
    head + body + '</div>';
}

// The calls and results among the loaded records, by call id, for the tool rows.
function runToolContext(records) {
  var calls = {};
  var results = {};
  records.forEach(function(rec) {
    if (!rec.call_id) return;
    if (rec.type === 'tool_call') calls[rec.call_id] = rec;
    if (rec.type === 'tool_result') results[rec.call_id] = rec;
  });
  return { calls: calls, results: results };
}

function runHeaderHtml(detail) {
  var hs = detail.header_summary || {};
  var sum = detail.summary || {};
  var persona = hs.persona || {};
  var tokens = sum.tokens || {};
  var tokenText = tokens.scope
    ? 'in ' + (tokens.input == null ? '-' : tokens.input) + ' · out ' + (tokens.output == null ? '-' : tokens.output) +
      ' · cache ' + (tokens.cache_read == null ? '-' : tokens.cache_read) + ' read, ' + (tokens.cache_write == null ? '-' : tokens.cache_write) + ' write'
    : (tokens.reason || 'no usage recorded');
  var outcome = sum.outcome || {};
  var outcomeText = outcome.status ? outcome.status : (outcome.reason || '-');
  var cost = sum.cost || {};
  var prHref = runsSafeHref(sum.pr_url);
  var prHtml = prHref
    ? '<a href="' + esc(prHref) + '" target="_blank" rel="noopener noreferrer">' + esc(prHref) + '</a>'
    : esc(sum.pr_reason || '-');
  var versionHtml = persona.version != null && personaHistoryPath(persona.name, persona.version)
    ? esc(String(persona.version)) + ' <button type="button" class="run-more" data-action="diff" data-role="' + esc(persona.name) +
      '" data-version="' + esc(String(persona.version)) + '">View diff</button>'
    : esc(persona.version != null ? String(persona.version) : (persona.reason || '-'));
  var rows = [
    ['Stage', hs.stage], ['Agent', hs.agent], ['Model', hs.model], ['Endpoint', hs.endpoint],
    ['Duration', fmtRunSeconds(sum.duration_seconds)], ['Started', hs.started_at || sum.started_at],
    ['Outcome', outcomeText]
  ];
  var list = rows.map(function(row) {
    return '<div class="run-fact"><span class="run-fact-label">' + esc(row[0]) + '</span><span>' + esc(row[1] == null ? '-' : row[1]) + '</span></div>';
  }).join('');
  return '<div class="run-facts">' + list +
    '<div class="run-fact"><span class="run-fact-label">Persona version</span><span>' + versionHtml + '</span></div>' +
    '<div class="run-fact"><span class="run-fact-label">Tokens</span><span>' + esc(tokenText) + '</span></div>' +
    '<div class="run-fact"><span class="run-fact-label">Cost</span><span title="' + esc(cost.reason || '') + '">' + esc(cost.value == null ? '-' : String(cost.value)) + '</span></div>' +
    '<div class="run-fact"><span class="run-fact-label">Pull request</span><span>' + prHtml + '</span></div>' +
    '</div>' +
    '<div class="run-downloads"><a href="' + esc(runDownloadHref(detail, 'jsonl')) + '" download>Download JSONL</a> · ' +
    '<a href="' + esc(runDownloadHref(detail, 'md')) + '" download>Download Markdown</a></div>';
}

function runDownloadHref(detail, extension) {
  var addr = { symphony: detail.symphony, card: detail.card, run: detail.run };
  return runApiBase(addr) + '/download.' + extension;
}

function runCardRunsHtml(runs, card) {
  if (!runs || runs.length === 0) return '<div class="muted">No transcripts for this card yet.</div>';
  return groupRunsByStage(runs).map(function(group) {
    var items = group.runs.map(function(run) {
      var outcome = run.outcome ? esc(run.outcome) : '';
      var when = run.started_at ? esc(String(run.started_at).slice(0, 19).replace('T', ' ')) : '-';
      var href = runPathFor(run.symphony, card, run.run);
      // 131: a conductor call is labelled with its decision; a performer run keeps its short id.
      var label = run.conductor
        ? 'Conductor call: ' + (run.conductor.decision || run.conductor.kind || 'no decision recorded')
        : run.run.slice(0, 12);
      return '<li><a href="' + esc(href) + '" onclick="navigate(event,this.getAttribute(\'href\'))">' +
        esc(label) + '</a> <span class="muted">' + when + '</span> ' + outcome + '</li>';
    }).join('');
    return '<div class="card-runs-stage"><strong>' + esc(group.stage) + '</strong><ul class="detail-list">' + items + '</ul></div>';
  }).join('');
}

// --- card detail: the runs per stage --------------------------------------- //

var _cardRunsCache = {};

// Fills every runs placeholder in a card detail. Results are cached briefly, so the
// per-second state refresh does not refetch the same list.
function hydrateCardRuns(container) {
  if (!container || !container.querySelectorAll) return;
  var nodes = container.querySelectorAll('.card-runs[data-card-id]');
  for (var i = 0; i < nodes.length; i++) {
    loadCardRunsInto(nodes[i], nodes[i].getAttribute('data-card-id'));
  }
}

function loadCardRunsInto(node, card) {
  if (!card || !RUN_NAME_PATTERN.test(card)) return;
  var cached = _cardRunsCache[card];
  if (cached && Date.now() - cached.at < RUN_CARD_CACHE_MS) {
    node.innerHTML = runCardRunsHtml(cached.runs, card);
    return;
  }
  fetch('/api/card-runs/' + encodeURIComponent(card), { credentials: 'same-origin' })
    .then(function(response) { return response.ok ? response.json() : { runs: [], available: false }; })
    .then(function(body) {
      _cardRunsCache[card] = { at: Date.now(), runs: body.runs || [] };
      if (node.isConnected !== false) node.innerHTML = body.available === false
        ? '<div class="muted">' + esc(body.reason || 'Transcripts are not available.') + '</div>'
        : runCardRunsHtml(body.runs || [], card);
    })
    .catch(function() { node.innerHTML = '<div class="muted">Runs could not be loaded.</div>'; });
}

// --- the page ---------------------------------------------------------------- //

var runPage = null;

function runEl(id) { return document.getElementById(id); }

function runSetNotice(text) {
  var notice = runEl('runs-notice');
  if (notice) notice.textContent = text || '';
}

function runFetchJson(url) {
  return fetch(url, { credentials: 'same-origin' }).then(function(response) {
    return response.json().then(function(body) { return { ok: response.ok, status: response.status, body: body }; });
  });
}

// Requests that arrive while one is in flight are queued, the latest wins, and nothing is
// dropped. A queued action runs once the in-flight request has settled (see runDrain).
function runQueueIfBusy(page, action) {
  if (!page.loading) return false;
  page.queued = action;
  return true;
}

// Always clears the loading flag, then runs the queued action, so a failed request cannot
// freeze paging. Called once per request, after its result has been applied.
function runDrain(page) {
  page.loading = false;
  var next = page.queued;
  page.queued = null;
  if (next && runPage === page) next();
}

// Opens the page for one path. A short /runs/<run> address is resolved first, then the
// browser address is replaced with the full one, so Back and reload both work.
function runsPageRoute(path, search) {
  var addr = runAddressFromPath(path);
  var page = runEl('runs-page');
  if (!page) return;
  runBindPage();
  if (addr.invalid) {
    runShowNotFound('That is not a transcript address.');
    return;
  }
  var at = runQueryParam(search, 'at');
  if (addr.kind === 'short') {
    runSetNotice('Finding the run...');
    runFetchJson('/api/run-lookup/' + encodeURIComponent(addr.run)).then(function(result) {
      if (!result.ok) { runShowNotFound('No transcript exists for this run.'); return; }
      var full = runPathFor(result.body.symphony, result.body.card, result.body.run);
      history.replaceState(null, '', full + (search || '') + (location.hash || ''));
      runOpen({ symphony: result.body.symphony, card: result.body.card, run: result.body.run }, at);
    }).catch(function() { runShowNotFound('The run could not be looked up.'); });
    return;
  }
  runOpen(addr, at);
}

function runShowNotFound(message) {
  var header = runEl('runs-header');
  if (header) header.innerHTML = '<div><strong>Run not found</strong></div><div class="muted">' + esc(message) + '</div>';
  var rows = runEl('runs-rows');
  if (rows) rows.innerHTML = '';
  runPage = null;
}

function runOpen(addr, at) {
  var seq = runSeqFromHash(location.hash);
  runPage = {
    addr: addr, detail: null, records: [], nextAfter: null, prevBefore: null,
    loading: false, queued: null, gen: 0,
    filter: { types: [], errors: false }, open: {}, measured: {}, remeasured: false,
    focusSeq: seq, focusAt: at || '', focusLine: null, errorCursor: 0,
    search: null, visible: []
  };
  var page = runPage;
  var scroller = runEl('runs-scroll');
  if (scroller) scroller.onscroll = runOnScroll;
  runSetNotice('Loading the run...');
  // A failed header does not stop the records: the events are the part that matters.
  runFetchJson(runApiBase(addr)).then(function(result) {
    if (runPage !== page) return;
    if (result.status === 404) { runShowNotFound('No transcript exists for this run.'); return; }
    if (result.ok) {
      page.detail = result.body;
      runEl('runs-header').innerHTML = runHeaderHtml(result.body);
      runStyleTitle(addr, result.body);
    } else {
      runEl('runs-header').innerHTML = '<div class="muted">The run header could not be loaded. Its events are below.</div>';
    }
    runLoadRecords(true);
  }).catch(function() {
    if (runPage !== page) return;
    runEl('runs-header').innerHTML = '<div class="muted">The run header could not be loaded. Its events are below.</div>';
    runLoadRecords(true);
  });
}

function runStyleTitle(addr, detail) {
  var hs = detail.header_summary || {};
  if (typeof setPageTitle === 'function') {
    setPageTitle('Run ' + (hs.stage || addr.run) + ' | Coordinare');
  }
}

function runRecordsQuery(page, extra) {
  var query = '?limit=' + RUN_PAGE_LIMIT;
  if (extra) query += extra;
  if (page.filter.types.length) query += '&types=' + encodeURIComponent(page.filter.types.join(','));
  if (page.filter.errors) query += '&errors=true';
  return query;
}

function runRecordsUrl(page, extra) {
  return runApiBase(page.addr) + '/records' + runRecordsQuery(page, extra);
}

// Loads the next page, or the first page again (reset). A focus (seq or time) positions the
// first page on that event. A reset requested during a load is queued, so a filter change
// is never lost.
function runLoadRecords(reset) {
  var page = runPage;
  if (!page) return;
  if (runQueueIfBusy(page, reset ? function() { runLoadRecords(true); } : null)) return;
  if (!reset && page.nextAfter == null) return;
  if (reset) {
    page.gen += 1;
    page.records = [];
    page.nextAfter = null;
    page.prevBefore = null;
  }
  var gen = page.gen;
  var extra = '';
  var asked = false;
  if (reset && page.focusSeq != null) { extra = '&seq=' + page.focusSeq; asked = true; }
  else if (reset && page.focusAt) { extra = '&at=' + encodeURIComponent(page.focusAt); asked = true; }
  else extra = '&after=' + (reset ? 0 : page.nextAfter);
  page.loading = true;
  runFetchJson(runRecordsUrl(page, extra)).then(function(result) {
    if (runPage !== page || page.gen !== gen) return;
    if (!result.ok) { runSetNotice('The records could not be loaded. Try again.'); return; }
    var body = result.body;
    page.records = page.records.concat(body.records || []);
    page.nextAfter = body.next_after;
    if (reset && body.focus_line != null) {
      page.focusLine = body.focus_line;
      page.focusSeq = null;
      page.focusAt = '';
    }
    if (reset && asked && body.focus_found === false) {
      runSetNotice('No event matches that link, so the run opens at its start.');
    }
    runTrimTail(page);
    runRender(reset && page.focusLine != null);
    if (!(reset && asked && body.focus_found === false)) runSetNotice(runCountText(page));
  }).catch(function() {
    if (runPage === page) runSetNotice('The records could not be loaded. Try again.');
  }).then(function() {
    if (runPage === page) runDrain(page);
  });
}

// The cap keeps the page bounded. Past it, the oldest events leave the page (they are one
// "Load earlier" away), so a long run is still fully reachable through the page.
function runTrimTail(page) {
  var extra = page.records.length - RUN_MAX_LOADED;
  if (extra <= 0) return;
  page.records = page.records.slice(extra);
  page.prevBefore = page.records[0].line;
}

function runTrimHead(page) {
  var extra = page.records.length - RUN_MAX_LOADED;
  if (extra <= 0) return;
  page.records = page.records.slice(0, RUN_MAX_LOADED);
  page.nextAfter = page.records[page.records.length - 1].line;
}

function runCountText(page) {
  var total = page.detail && page.detail.summary ? page.detail.summary.records : null;
  var shown = filterRecords(page.records, page.filter).length;
  var text = shown + ' of ' + (total == null ? '?' : total) + ' records';
  if (page.prevBefore != null) text += ', earlier events above';
  if (page.nextAfter != null) text += ', more below';
  return text;
}

// Pages back, for events above the loaded window. The anchor row stays where it was.
function runLoadEarlier() {
  var page = runPage;
  if (!page || page.prevBefore == null) return;
  if (runQueueIfBusy(page, runLoadEarlier)) return;
  var gen = page.gen;
  var anchor = page.records.length ? page.records[0].line : null;
  page.loading = true;
  runFetchJson(runRecordsUrl(page, '&before=' + page.prevBefore)).then(function(result) {
    if (runPage !== page || page.gen !== gen) return;
    if (!result.ok) { runSetNotice('Earlier events could not be loaded. Try again.'); return; }
    page.records = (result.body.records || []).concat(page.records);
    page.prevBefore = result.body.prev_before;
    runTrimHead(page);
    page.focusLine = anchor;
    page.focusSeq = null;
    runRender(true);
    runSetNotice(runCountText(page));
  }).catch(function() {
    if (runPage === page) runSetNotice('Earlier events could not be loaded. Try again.');
  }).then(function() {
    if (runPage === page) runDrain(page);
  });
}

function runOnScroll() {
  var scroller = runEl('runs-scroll');
  if (!scroller || !runPage) return;
  runRender(false);
  var page = runPage;
  var nearEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - scroller.clientHeight;
  if (nearEnd && page.nextAfter != null && !page.loading) runLoadRecords(false);
  var nearTop = scroller.scrollTop < scroller.clientHeight / 2;
  if (nearTop && page.prevBefore != null && !page.loading) runLoadEarlier();
}

// Draws only the rows in the window. Heights are measured after each draw and cached by
// line, so an expanded row takes its real height. One re-draw follows a change, at most.
function runRender(scrollToFocus) {
  var scroller = runEl('runs-scroll');
  var rows = runEl('runs-rows');
  var page = runPage;
  if (!scroller || !rows || !page) return;
  var list = filterRecords(page.records, page.filter);
  page.visible = list;
  var heights = list.map(function(rec) { return page.measured[rec.line] || RUN_ROW_ESTIMATE; });
  if (scrollToFocus) {
    var target = runIndexOfFocus(list, page);
    if (target >= 0) {
      var offset = 0;
      for (var h = 0; h < target; h++) offset += heights[h];
      scroller.scrollTop = offset;
      runMarkFocus(list[target]);
    }
  }
  var win = windowFor(heights, scroller.scrollTop, scroller.clientHeight || 600, RUN_WINDOW_MARGIN);
  var ctx = runToolContext(page.records);
  ctx.open = page.open;
  var html = '<div class="run-spacer" style="height:' + win.padTop + 'px"></div>';
  for (var i = win.start; i < win.end; i++) html += runRowHtml(list[i], ctx);
  html += '<div class="run-spacer" style="height:' + win.padBottom + 'px"></div>';
  rows.innerHTML = html;
  runMeasure(rows, page);
  if (scrollToFocus) runMarkFocus(runFocusRecord(list, page));
}

function runFocusRecord(list, page) {
  var index = runIndexOfFocus(list, page);
  return index >= 0 ? list[index] : null;
}

function runMarkFocus(rec) {
  if (!rec) return;
  var rows = runEl('runs-rows');
  if (!rows) return;
  var nodes = rows.querySelectorAll('.run-row.run-focus');
  for (var i = 0; i < nodes.length; i++) nodes[i].classList.remove('run-focus');
  var anchor = rec.seq != null ? 'e' + rec.seq : 'l' + rec.line;
  var node = runEl(anchor);
  if (node && node.classList) node.classList.add('run-focus');
}

function runIndexOfFocus(list, page) {
  for (var i = 0; i < list.length; i++) {
    if (page.focusSeq != null && list[i].seq === page.focusSeq) return i;
    if (page.focusLine != null && list[i].line === page.focusLine) return i;
  }
  return -1;
}

function runMeasure(rows, page) {
  var changed = false;
  var nodes = rows.querySelectorAll('.run-row[data-line]');
  for (var i = 0; i < nodes.length; i++) {
    var line = parseInt(nodes[i].getAttribute('data-line'), 10);
    var height = nodes[i].offsetHeight;
    if (height > 0 && Math.abs((page.measured[line] || RUN_ROW_ESTIMATE) - height) > 1) {
      page.measured[line] = height;
      changed = true;
    }
  }
  if (changed && !page.remeasured) {
    page.remeasured = true;
    runRender(false);
  } else {
    page.remeasured = false;
  }
}

// One delegated listener for every control in the page. Values come from data attributes
// that this file wrote, and each is checked before use.
function runBindPage() {
  var rows = runEl('runs-rows');
  var page = runEl('runs-page');
  if (rows && !rows._runBound) {
    rows._runBound = true;
    rows.addEventListener('toggle', function(event) {
      var target = event.target;
      if (!runPage || !target || !target.getAttribute) return;
      var line = target.getAttribute('data-line');
      if (line) runPage.open[line] = target.open;
    }, true);
    rows.addEventListener('click', runRowClick);
  }
  if (page && !page._runBound) {
    page._runBound = true;
    page.addEventListener('click', runPageClick);
    var typeSelect = runEl('runs-type-filter');
    if (typeSelect && typeSelect.options.length === 1) {
      RUN_TYPE_FILTERS.forEach(function(type) {
        var option = document.createElement('option');
        option.value = type;
        option.textContent = recordKindLabel({ type: type });
        typeSelect.appendChild(option);
      });
    }
    var search = runEl('runs-search-form');
    if (search) search.addEventListener('submit', function(event) { event.preventDefault(); runSearch(false, false); });
    var goto = runEl('runs-goto-form');
    if (goto) goto.addEventListener('submit', function(event) { event.preventDefault(); runGotoSeq(); });
    var filter = runEl('runs-type-filter');
    if (filter) filter.addEventListener('change', runFilterChanged);
    var errors = runEl('runs-errors-filter');
    if (errors) errors.addEventListener('change', runFilterChanged);
  }
}

function runRowClick(event) {
  var target = event.target;
  if (!target || !target.getAttribute) return;
  var action = target.getAttribute('data-action');
  if (action === 'full') runLoadFull(target);
  else if (action === 'copy') runCopyLink(target);
}

function runPageClick(event) {
  var target = event.target;
  if (!target || !target.getAttribute || !runPage) return;
  var action = target.getAttribute('data-action');
  if (action === 'jump') runJumpToLine(parseInt(target.getAttribute('data-line'), 10));
  else if (action === 'diff') runShowDiff(target.getAttribute('data-role'), parseInt(target.getAttribute('data-version'), 10));
  else if (action === 'next-error') runSearch(true, false);
  else if (action === 'more-matches') runSearch(false, true);
  else if (action === 'earlier') runLoadEarlier();
}

function runLoadFull(button) {
  var line = parseInt(button.getAttribute('data-line'), 10);
  var field = button.getAttribute('data-field');
  if (!runPage || isNaN(line) || RUN_FULL_FIELDS.indexOf(field) === -1) return;
  var page = runPage;
  button.disabled = true;
  button.textContent = 'Loading...';
  runFetchJson(runApiBase(page.addr) + '/records/' + line + '/' + encodeURIComponent(field)).then(function(result) {
    if (runPage !== page) return;
    if (!result.ok) { button.textContent = 'Could not load it'; return; }
    var rec = null;
    for (var i = 0; i < page.records.length; i++) {
      if (page.records[i].line === line) { rec = page.records[i]; break; }
    }
    if (rec) {
      rec[field] = result.body.value;
      rec[field + '_truncated'] = false;
      rec[field + '_format'] = result.body.format;
    }
    runRender(false);
  }).catch(function() { button.textContent = 'Could not load it'; });
}

function runCopyLink(button) {
  if (!runPage) return;
  var seq = parseInt(button.getAttribute('data-seq'), 10);
  var url = runCopyLinkUrl(location.origin, runPage.addr.symphony, runPage.addr.card, runPage.addr.run, seq);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(
      function() { runSetNotice('Link copied: ' + url); },
      function() { runSetNotice('Copy this link: ' + url); }
    );
  } else {
    runSetNotice('Copy this link: ' + url);
  }
}

function runFilterChanged() {
  if (!runPage) return;
  var select = runEl('runs-type-filter');
  var types = [];
  if (select && select.value) types.push(select.value);
  var errors = runEl('runs-errors-filter');
  runPage.filter = { types: types, errors: !!(errors && errors.checked) };
  runPage.focusSeq = null;
  runPage.focusAt = '';
  runPage.focusLine = null;
  runLoadRecords(true);
}

// Jumps to an event by its seq (the number in the row's head).
function runGotoSeq() {
  var page = runPage;
  var input = runEl('runs-goto');
  var seq = input ? parseInt(input.value, 10) : NaN;
  if (!page || isNaN(seq) || seq < 0) { runSetNotice('Enter an event number.'); return; }
  page.focusSeq = seq;
  page.focusAt = '';
  page.focusLine = null;
  runLoadRecords(true);
}

// Jumps to one line. An event already loaded is scrolled to. Otherwise the window is
// rebuilt around it: the events before it (so Load earlier still works) and from it on.
function runJumpToLine(line) {
  var page = runPage;
  if (!page || isNaN(line)) return;
  var found = null;
  for (var i = 0; i < page.records.length; i++) {
    if (page.records[i].line === line) { found = page.records[i]; break; }
  }
  if (found) {
    page.focusSeq = found.seq;
    page.focusLine = found.line;
    runRender(true);
    return;
  }
  if (runQueueIfBusy(page, function() { runJumpToLine(line); })) return;
  var gen = page.gen;
  page.loading = true;
  page.focusSeq = null;
  page.focusLine = line;
  page.focusAt = '';
  Promise.all([
    runFetchJson(runRecordsUrl(page, '&after=' + Math.max(0, line - 1))),
    runFetchJson(runRecordsUrl(page, '&before=' + line)),
  ]).then(function(results) {
    if (runPage !== page || page.gen !== gen) return;
    var forward = results[0];
    var backward = results[1];
    if (!forward.ok || !backward.ok) { runSetNotice('That event could not be loaded. Try again.'); return; }
    page.records = (backward.body.records || []).concat(forward.body.records || []);
    page.nextAfter = forward.body.next_after;
    page.prevBefore = backward.body.prev_before;
    runTrimTail(page);
    runRender(true);
    runSetNotice(runCountText(page));
  }).catch(function() {
    if (runPage === page) runSetNotice('That event could not be loaded. Try again.');
  }).then(function() {
    if (runPage === page) runDrain(page);
  });
}

// Searches the whole run, or continues a search from its cursor. "Next error" searches for
// failures after the last one reached. Each search stops at a byte budget and says so.
function runSearch(fromError, more) {
  var page = runPage;
  if (!page) return;
  if (more && !page.search) return;
  if (!more && runQueueIfBusy(page, function() { runSearch(fromError, false); })) return;
  var input = runEl('runs-search');
  var query = fromError ? '' : (more ? page.search.query : (input ? input.value : ''));
  var errors = fromError || (more ? page.search.errors : false);
  var after = more ? page.search.next : (fromError ? page.errorCursor : 0);
  var url = runApiBase(page.addr) + '/search?q=' + encodeURIComponent(query) +
    '&limit=' + RUN_SEARCH_LIMIT + '&after=' + after + (errors ? '&errors=true' : '');
  var results = runEl('runs-search-results');
  if (results && !more) results.textContent = 'Searching...';
  runFetchJson(url).then(function(result) {
    if (runPage !== page) return;
    if (!result.ok) { if (results) results.textContent = 'The search could not be run.'; return; }
    var body = result.body;
    var matches = body.matches || [];
    var previous = more && page.search ? page.search.matches : [];
    page.search = {
      query: query, errors: errors, matches: previous.concat(matches),
      next: body.next_after, stoppedAt: body.stopped_at_byte,
    };
    if (fromError) {
      if (!matches.length) { runSetNotice('No further errors in this run.'); return; }
      page.errorCursor = matches[0].line;
      runJumpToLine(matches[0].line);
      return;
    }
    runRenderSearch(page);
  }).catch(function() {
    if (runPage === page && results) results.textContent = 'The search could not be run.';
  });
}

function runRenderSearch(page) {
  var box = runEl('runs-search-results');
  if (!box) return;
  var state = page.search || { matches: [], next: null, stoppedAt: null };
  var html = '';
  if (!state.matches.length) {
    html = '<div class="muted">No matches' + (state.next != null ? ' yet' : '') + '.</div>';
  } else {
    html = state.matches.map(function(m) {
      return '<button type="button" class="run-match" data-action="jump" data-line="' + esc(String(m.line)) + '">' +
        esc(recordKindLabel(m)) + ' ' + esc(m.seq == null ? 'line ' + m.line : '#' + m.seq) + ': ' + esc(recordTitle(m)) + '</button>';
    }).join('');
  }
  if (state.next != null) {
    var note = state.stoppedAt != null
      ? 'The search stopped after ' + esc(String(state.stoppedAt)) + ' bytes of the run.'
      : 'More matches follow.';
    html += '<div class="muted">' + note + '</div>' +
      '<button type="button" class="run-more" data-action="more-matches">Load more matches</button>';
  }
  box.innerHTML = html;
}

function runShowDiff(role, version) {
  var url = personaHistoryPath(role, version);
  var box = runEl('runs-diff');
  if (!url || !box) return;
  box.innerHTML = '<div class="muted">Loading the persona diff...</div>';
  runFetchJson(url).then(function(result) {
    if (!result.ok) { box.innerHTML = '<div class="muted">That persona version is no longer retained.</div>'; return; }
    box.innerHTML = '<div class="run-io-label">Persona ' + esc(role) + ' version ' + esc(String(version)) + '</div>' +
      '<pre class="run-pre run-diff">' + esc(result.body.diff || 'This is the first retained version.') + '</pre>';
  });
}
