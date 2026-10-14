// 147: the card pipeline timeline. The tracker, the stage detail and the lifecycle list in the
// card detail; the dots on a board card; and the one read behind them,
// /api/cards/<symphony>/<card>/timeline. The render functions take data and return markup, and
// every interpolated value goes through esc(). The fetch is cached briefly, so a snapshot that
// re-renders the card detail does not read the transcripts again. Served from /static/timeline.js.

var TIMELINE_CACHE_MS = 10000;
var _timelineCache = {};

var TIMELINE_STATUS_TEXT = {
  done: 'Done',
  current: 'In progress',
  failed: 'Changes requested',
  ran: 'Ran, no verdict recorded',
  skipped: 'Skipped',
  'pending': 'Not reached yet',
  not_recorded: 'Not recorded'
};

var TIMELINE_STATUS_ICON = {
  done: '✓',
  current: '●',
  failed: '✗',
  ran: '◐',
  skipped: '○',
  'pending': '·',
  not_recorded: '?'
};

var TIMELINE_EVENT_TEXT = {
  dispatched: 'Dispatched',
  feedback_relayed: 'Feedback relayed',
  verdict: 'Verdict',
  ci_result: 'CI gate',
  pr_opened: 'Pull request opened',
  review_received: 'Review received',
  human_action: 'Human action',
  override: 'Override',
  merged: 'Merged'
};

function timelineOwn(map, key) {
  return map && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
}

function timelineStatusText(status) {
  return timelineOwn(TIMELINE_STATUS_TEXT, status) || String(status || '');
}

function timelineStatusIcon(status) {
  return timelineOwn(TIMELINE_STATUS_ICON, status) || '?';
}

// "2m 5s", "1h 3m": a duration from recorded timestamps. '' when it is not known.
function timelineDuration(secs) {
  var n = Number(secs);
  if (secs === null || secs === undefined || !isFinite(n) || n < 0) return '';
  var total = Math.round(n);
  if (total < 60) return total + 's';
  var mins = Math.floor(total / 60);
  if (mins < 60) return mins + 'm ' + (total % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

// The run count with its source. None recorded is said, never shown as zero.
function timelineRunsText(stage) {
  if (stage.run_count === null || stage.run_count === undefined) return 'runs not recorded';
  var n = Number(stage.run_count);
  var word = n === 1 ? 'run' : 'runs';
  var source = stage.run_count_source === 'lifecycle' ? ' (from events)' : '';
  var floor = stage.run_count_at_least ? 'at least ' : '';
  return floor + n + ' ' + word + source;
}

// "passed", "changes requested" and the marker, or "no verdict" when none is recorded.
function timelineVerdictText(verdict) {
  if (!verdict) return 'no verdict';
  var result = verdict.result === 'pass' ? 'passed' : (verdict.result === 'fail' ? 'changes requested' : 'recorded');
  var marker = String(verdict.marker || '').replace(/_/g, ' ');
  return marker && marker !== result ? result + ' (' + marker + ')' : result;
}

// The cost and tokens of one stage, from the ledger. A stage with no ledger figures says why.
function timelineUsageOf(stage) {
  if (stage.usage) return stage.usage;
  return { tracked: false, reason: stage.usage_reason || 'Token and cost figures are not tracked.' };
}

function timelineCostHtml(stage) {
  var readout = usageCostReadout(timelineUsageOf(stage));
  return '<span title="' + esc(readout.title) + '">' + esc(readout.text) + '</span>';
}

function timelineTokensHtml(stage) {
  var readout = usageTokensReadout(timelineUsageOf(stage), null);
  return '<span title="' + esc(readout.title) + '">' + esc(readout.text) + '</span>';
}

// One stage in the tracker: its icon, label, duration, runs, verdict, tokens and cost.
function timelineCellHtml(stage, selected) {
  var status = String(stage.status || '');
  var classes = 'tl-stage tl-' + esc(status) + (selected ? ' tl-selected' : '');
  var duration = timelineDuration(stage.duration_seconds);
  var durationHtml = duration
    ? esc(duration) + (stage.duration_partial ? ' (partial)' : '')
    : '<span class="muted">duration not recorded</span>';
  return '<button type="button" class="' + classes + '" data-stage="' + esc(stage.stage) + '"' +
    ' aria-pressed="' + (selected ? 'true' : 'false') + '" title="' + esc(timelineStatusText(status)) + '"' +
    ' onclick="timelineSelectStage(this)">' +
    '<span class="tl-icon" aria-hidden="true">' + esc(timelineStatusIcon(status)) + '</span>' +
    '<span class="tl-name">' + stageLabel(stage.stage) + '</span>' +
    '<span class="tl-status">' + esc(timelineStatusText(status)) + '</span>' +
    '<span class="tl-line">' + durationHtml + '</span>' +
    '<span class="tl-line">' + esc(timelineRunsText(stage)) + '</span>' +
    '<span class="tl-line">' + esc(timelineVerdictText(stage.verdict)) + '</span>' +
    '<span class="tl-line">' + timelineTokensHtml(stage) + '</span>' +
    '<span class="tl-line">' + timelineCostHtml(stage) + '</span>' +
    '</button>';
}

// The horizontal tracker: one cell per pipeline stage, skipped stages greyed out.
function timelineTrackerHtml(tl, selectedStage) {
  var stages = Array.isArray(tl.stages) ? tl.stages : [];
  return '<div class="tl-tracker" role="group" aria-label="Pipeline stages">' +
    stages.map(function(stage) { return timelineCellHtml(stage, stage.stage === selectedStage); }).join('') +
    '</div>';
}

// One run of a stage: its short name links to the transcript (133), then its time, length and outcome.
function timelineRunHref(symphony, card, run) {
  return '/runs/' + encodeURIComponent(symphony) + '/' + encodeURIComponent(card) + '/' + encodeURIComponent(run);
}

// Why a run has no end time: the byte budget ran out before its file was read, it is still
// running, or its file could not be read. A finished run with no recorded duration says that.
var TIMELINE_READ_TEXT = {
  budget: 'not read (budget)',
  running: 'still running',
  unreadable: 'not readable'
};

function timelineRunRowHtml(tl, run) {
  var href = timelineRunHref(tl.symphony, tl.card_id, run.run);
  var duration = timelineDuration(run.duration_seconds);
  var readText = timelineOwn(TIMELINE_READ_TEXT, run.read_state) || 'duration not recorded';
  var outcome = run.outcome === 'failed' ? 'failed' : (run.outcome === 'completed' ? 'completed' : 'outcome not recorded');
  return '<li class="tl-run">' + runIdHtml(run.run, { href: href }) +
    ' <span class="muted">' + esc(run.started_at ? fmtTime(run.started_at) : 'start not recorded') + '</span>' +
    ' <span>' + esc(duration || readText) + '</span>' +
    ' <span class="muted">' + esc(run.read_state === 'done' || !run.read_state ? outcome : '') + '</span></li>';
}

// The detail under the tracker for the stage picked: its runs, verdict, tokens and cost.
function timelineStageDetailHtml(tl, stageCode) {
  var stages = Array.isArray(tl.stages) ? tl.stages : [];
  var stage = null;
  stages.forEach(function(item) { if (item.stage === stageCode) stage = item; });
  if (!stage) return '';
  var head = '<div class="tl-detail-head">' + stageAgentLabel(stage.stage) + ' · ' +
    esc(timelineStatusText(stage.status)) + ' · ' + esc(timelineVerdictText(stage.verdict)) + '</div>';
  var runs;
  if (!tl.runs_available) {
    runs = '<div class="muted">' + esc(tl.runs_reason || 'Run transcripts are not available.') + '</div>';
  } else if (!stage.runs || stage.runs.length === 0) {
    runs = '<div class="muted">No runs recorded for this stage.</div>';
  } else {
    runs = '<ul class="tl-runs">' + stage.runs.map(function(run) { return timelineRunRowHtml(tl, run); }).join('') + '</ul>';
  }
  var money = '<div class="muted">Tokens ' + timelineTokensHtml(stage) + ' · cost ' + timelineCostHtml(stage) + '</div>';
  return '<div class="tl-detail">' + head + runs + money + '</div>';
}

// One lifecycle event, as a line of text. Free text from the server is escaped here.
function timelineEventHtml(event) {
  var kind = String(event.kind || '');
  var detail = event.detail || {};
  var text = timelineOwn(TIMELINE_EVENT_TEXT, kind) || kind;
  var extra = '';
  if (kind === 'verdict') extra = ': ' + timelineVerdictText({ result: detail.result, marker: detail.marker });
  else if (kind === 'pr_opened' && detail.pr_number !== undefined && detail.pr_number !== null) extra = ' #' + String(detail.pr_number);
  else if (kind === 'ci_result') extra = ': ' + String(detail.result || '').toLowerCase();
  else if (kind === 'review_received') extra = ': ' + String(detail.reviewer || '').toLowerCase() + ' ' + String(detail.review_state || '').toLowerCase().replace(/_/g, ' ');
  else if (kind === 'feedback_relayed' && detail.items !== undefined) extra = ' (' + String(detail.items) + (Number(detail.items) === 1 ? ' item)' : ' items)');
  else if (kind === 'human_action' || kind === 'override') extra = detail.action ? ': ' + String(detail.action) : '';
  var stage = event.stage ? ' · ' + stageLabel(event.stage) : '';
  return '<li class="tl-event tl-event-' + esc(kind) + '"><span class="muted">' + esc(fmtTime(event.at)) + '</span> ' +
    esc(text + extra) + stage + '</li>';
}

// The vertical lifecycle list, oldest first. An empty list says so; it does not guess.
function timelineEventsHtml(tl) {
  var events = Array.isArray(tl.events) ? tl.events : [];
  if (events.length === 0) return '<div class="muted">No lifecycle events recorded for this card.</div>';
  return '<ol class="tl-events">' + events.map(timelineEventHtml).join('') + '</ol>';
}

// What the timeline could not show, in the server's words.
function timelineNotesHtml(tl) {
  var notes = Array.isArray(tl.not_recorded) ? tl.not_recorded : [];
  if (notes.length === 0) return '';
  return '<ul class="tl-notes muted">' + notes.map(function(note) { return '<li>' + esc(note) + '</li>'; }).join('') + '</ul>';
}

// The board card's compact strip: one dot per stage, the current one ringed.
function timelineDotsHtml(strip) {
  if (!strip || !Array.isArray(strip.statuses)) return '';
  var dots = strip.statuses.map(function(item) {
    var label = vocabularyKindLabel('stage_labels', item.stage);
    var current = item.stage === strip.current ? ' tl-dot-current' : '';
    return '<span class="tl-dot tl-' + esc(item.status) + current + '" title="' +
      esc(label + ': ' + timelineStatusText(item.status)) + '"></span>';
  }).join('');
  return '<span class="tl-dots" title="Pipeline stages">' + dots + '</span>';
}

// The strip of one card, from the snapshot's card_pipeline (any symphony), or null.
function cardPipelineOf(s, cardId) {
  var all = s && s.card_pipeline && typeof s.card_pipeline === 'object' ? s.card_pipeline : {};
  for (var symphony in all) {
    if (timelineOwn(all, symphony) && timelineOwn(all[symphony], cardId)) return all[symphony][cardId];
  }
  return null;
}

// The symphony a card belongs to in this view, or the only one when there is just one.
function timelineSymphonyFor(s, cardId) {
  var owner = symphonyOfCard(s, cardId);
  if (owner) return owner;
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  return syms.length === 1 && syms[0] && syms[0].name ? String(syms[0].name) : '';
}

function timelineUrl(symphony, card) {
  return '/api/cards/' + encodeURIComponent(symphony) + '/' + encodeURIComponent(card) + '/timeline';
}

// The stage the detail shows first: the one running now, else the last one that ran.
function timelineDefaultStage(tl) {
  if (tl.current_stage) return tl.current_stage;
  var picked = '';
  (tl.stages || []).forEach(function(stage) {
    if (stage.status === 'done' || stage.status === 'failed' || stage.status === 'ran') picked = stage.stage;
  });
  return picked;
}

// Writes the whole timeline into one container. The stage picked is kept on the container.
function timelineRender(node, tl, selectedStage) {
  var picked = selectedStage || timelineDefaultStage(tl);
  node.setAttribute('data-selected', picked);
  node.timelineBody = tl;
  node.innerHTML = '<div class="tl-heading">Pipeline</div>' +
    timelineTrackerHtml(tl, picked) +
    timelineStageDetailHtml(tl, picked) +
    '<div class="tl-heading">Lifecycle</div>' +
    timelineEventsHtml(tl) +
    timelineNotesHtml(tl);
}

// The tracker's button: picks a stage and redraws the detail from the body already loaded.
function timelineSelectStage(button) {
  var node = button;
  while (node && !(node.classList && node.classList.contains('card-timeline'))) node = node.parentNode;
  if (!node || !node.timelineBody) return;
  timelineRender(node, node.timelineBody, button.getAttribute('data-stage'));
}

// Loads one card's timeline into a container, from the short cache when it is fresh.
function timelineLoad(node, symphony, card) {
  var key = JSON.stringify([symphony, card]);
  var hit = _timelineCache[key];
  if (hit && Date.now() - hit.at < TIMELINE_CACHE_MS) {
    timelineRender(node, hit.body, node.getAttribute('data-selected'));
    return;
  }
  fetch(timelineUrl(symphony, card), { credentials: 'same-origin' })
    .then(function(response) {
      return response.json().then(function(body) { return { ok: response.ok, body: body }; });
    })
    .then(function(res) {
      if (!res.ok) {
        if (node.isConnected !== false) node.innerHTML = '<div class="muted">' + esc((res.body && res.body.detail) || 'The timeline could not be loaded.') + '</div>';
        return;
      }
      _timelineCache[key] = { at: Date.now(), body: res.body };
      if (node.isConnected !== false) timelineRender(node, res.body, node.getAttribute('data-selected'));
    })
    .catch(function() {
      if (node.isConnected !== false) node.innerHTML = '<div class="muted">The timeline could not be loaded.</div>';
    });
}

// Loads every timeline placeholder under a node. Placeholders carry their symphony and card.
function timelineHydrate(root) {
  if (!root || !root.querySelectorAll) return;
  var nodes = root.querySelectorAll('.card-timeline');
  for (var i = 0; i < nodes.length; i++) {
    var symphony = nodes[i].getAttribute('data-symphony') || '';
    var card = nodes[i].getAttribute('data-card') || '';
    if (card) timelineLoad(nodes[i], symphony, card);
  }
}

// The placeholder the card detail writes; timelineHydrate fills it.
function timelinePlaceholderHtml(symphony, card) {
  return '<div class="card-timeline" data-symphony="' + esc(symphony) + '" data-card="' + esc(card) +
    '"><div class="muted">Loading timeline&hellip;</div></div>';
}
