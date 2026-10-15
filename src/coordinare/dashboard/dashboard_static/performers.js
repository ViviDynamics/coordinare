// 349: performers-page rendering, the card-detail panels it shares, and the
// token-total derivation, served verbatim from /static/performers.js.
// The dead session-stats token branch is gone on purpose: SessionStats has
// never carried a token count, so that branch could never fire.
function parseTokenCount(text) {
  var msg = String(text || '');
  var tagged = msg.match(/([\d,]+)\s+tokens\s+total/i);
  if (tagged && tagged[1]) {
    var taggedNum = parseInt(tagged[1].replace(/,/g, ''), 10);
    if (!isNaN(taggedNum)) return taggedNum;
  }
  var generic = msg.match(/([\d,]+)/);
  if (generic && generic[1]) {
    var genericNum = parseInt(generic[1].replace(/,/g, ''), 10);
    if (!isNaN(genericNum)) return genericNum;
  }
  return null;
}

function derivePerformerTokenTotal(s) {
  var m = s.performer_metrics || {};
  if (typeof m.tokens_processed === 'number' && isFinite(m.tokens_processed) && m.tokens_processed >= 0) {
    return Math.floor(m.tokens_processed);
  }
  var events = Array.isArray(s.performer_events) ? s.performer_events : [];
  for (var i = events.length - 1; i >= 0; i--) {
    var ev = events[i] || {};
    if ((ev.type || '') !== 'cost') continue;
    var parsed = parseTokenCount(ev.text || '');
    if (parsed != null) return parsed;
  }
  if (typeof s.card_tokens_total === 'number' && isFinite(s.card_tokens_total) && s.card_tokens_total > 0) {
    return Math.floor(s.card_tokens_total);
  }
  return null;
}

// 123: a busy role is not an error. A role at capacity keeps the normal bar
// colour; the bar is highlighted only when work is queued behind it.
function utilizationBarColor(r) {
  return Number(r && r.queued) > 0 ? 'var(--color-accent-yellow)' : 'var(--color-bar-fill)';
}

// 154: the role's busy bar. The Performers page table carries it; the Dashboard shows a summary line.
function utilizationBarHtml(r) {
  var max = Number(r.max) || 0;
  var active = Number(r.active) || 0;
  var pct = max > 0 ? Math.min(100, Math.round(active / max * 100)) : 0;
  return '<div style="background:var(--color-bar-track);border-radius:3px;height:8px;width:60px;display:inline-block;vertical-align:middle"><div style="background:' + utilizationBarColor(r) + ';height:100%;width:' + pct + '%;border-radius:3px"></div></div>';
}

// 124: users see the issue number, never the raw ProjectV2 item id. The node
// id stays reachable on hover for anyone who needs to quote it.
function cardRefHtml(sess) {
  sess = sess || {};
  var nodeId = String(sess.card_id || '');
  var number = sess.issue_number;
  var label = number ? '#' + String(number) : 'Card';
  var safeUrl = sess.issue_url && /^https?:\/\//i.test(sess.issue_url);
  var inner = safeUrl
    ? '<a href="' + esc(sess.issue_url) + '" target="_blank" rel="noopener">' + esc(label) + '</a>'
    : esc(label);
  var html = '<span class="ap-pill ap-pill-card" title="' + esc(nodeId ? 'Card ' + nodeId : '') + '"><strong>' + inner + '</strong></span>';
  if (sess.pr_url && /^https?:\/\//i.test(sess.pr_url)) {
    var prNumber = (String(sess.pr_url).match(/\/pull\/(\d+)/) || [])[1];
    html += '<span class="ap-pill ap-pill-pr"><strong><a href="' + esc(sess.pr_url) + '" target="_blank" rel="noopener">PR' + (prNumber ? ' #' + esc(prNumber) : '') + '</a></strong></span>';
  }
  return html;
}

// 125: every elapsed readout for a stage run counts from the same moment (the
// dispatch of that run) and is refreshed by one timer, so two readouts for the
// same run can never disagree. The element carries its start point.
function elapsedSinceHtml(iso) {
  return '<strong>' + timeHtml(iso) + '</strong>';
}

function refreshElapsedReadouts(root) {
  // 157: the ages themselves are refreshed by refreshRelativeTimes, so only the headline is left here.
  // 138: the same tick keeps the headline's "next check in Ns" counting down.
  if (typeof refreshConductorHeadline === 'function') refreshConductorHeadline();
}

// 128: consecutive checks with the same phase and outcome are one row.
function collapseCycleRows(history) {
  var rows = [];
  (Array.isArray(history) ? history : []).forEach(function(e) {
    var outcome = e.outcome || 'success';
    var last = rows[rows.length - 1];
    var duration = typeof e.duration_seconds === 'number' && isFinite(e.duration_seconds) ? e.duration_seconds : null;
    if (last && last.phase === e.phase && last.outcome === outcome) {
      last.count += 1;
      last.last_timestamp = e.timestamp;
      if (duration != null) { last.duration_total += duration; last.duration_count += 1; }
      return;
    }
    rows.push({
      phase: e.phase, outcome: outcome, count: 1,
      timestamp: e.timestamp, last_timestamp: e.timestamp,
      duration_total: duration != null ? duration : 0, duration_count: duration != null ? 1 : 0,
    });
  });
  rows.forEach(function(r) {
    r.duration_seconds = r.duration_count > 0 ? r.duration_total / r.duration_count : null;
  });
  return rows;
}

// 134: a card's or a run's figures are the daemon's usage summary (usage.js).
// Returns that summary for one scope, the "not tracked" marker, or null.
function usageSummaryOf(sess, scope) {
  var u = sess && sess.usage;
  if (!u) return null;
  if (u.tracked === false) return u;
  return u[scope] || null;
}

// The card's cost as {value, basis}. value is 0 when no priced cost is known;
// basis is the tooltip text. Nothing here prices tokens with a flat rate.
function estimateCost(sess, s) {
  var summary = usageSummaryOf(sess, 'card');
  var readout = usageCostReadout(summary);
  return { value: summary && !readout.unknown ? Number(summary.cost.usd) || 0 : 0, basis: readout.title };
}

// 122: a stage that is running has usage the backend has not reported yet.
// Most backends report tokens only when the stage ends, so a literal 0 during
// the run is false. Show an explicit pending state instead.
var USAGE_PENDING_NOTE = 'Reported when the stage ends';

function isStageRunning(sess) {
  var phase = sess && sess.phase;
  return phase === 'monitoring_performer' || phase === 'monitoring_agent';
}

// Returns {text, title, pending}. `tokens` is the count on screen (may be
// null); `running` says whether a stage run is in progress for it.
function tokenReadout(tokens, running) {
  var n = Number(tokens);
  if (isFinite(n) && n > 0) return { text: Math.floor(n).toLocaleString(), title: '', pending: false };
  if (running) return { text: 'no usage yet', title: USAGE_PENDING_NOTE, pending: true };
  return { text: '0', title: '', pending: false };
}

function costHtml(sess, s) {
  var summary = usageSummaryOf(sess, 'card');
  // 122: a stage that is running has usage the backend has not reported yet.
  if (isStageRunning(sess) && usageTrackedSummary(summary) && !(summary.tokens.total > 0)) {
    return '<span title="' + esc(USAGE_PENDING_NOTE) + '">-</span>';
  }
  return usageCostHtml(summary);
}

// 149: every session skip reason on the page. The top-level map is restored to
// its pre-cycle value after a symphony runs, so in symphony mode each board's
// own map (symphonies[].state.session_skip_reasons) holds the live reasons.
// Card ids are unique across boards, so the maps merge without collisions.
function sessionSkipReasons(s) {
  var merged = Object.assign({}, (s && s.session_skip_reasons) || {});
  ((s && s.symphonies) || []).forEach(function(sym) {
    Object.assign(merged, (sym && sym.state && sym.state.session_skip_reasons) || {});
  });
  return merged;
}

// Which limit is keeping a card queued, in plain words. null for any other
// skip reason, so callers keep their ordinary label. The words are shared with
// the swimlane's wait line (waiting.js queuedWaitText), so they cannot differ.
function queueLimitLabel(skip) {
  var r = skip || {};
  return queuedWaitText(r.reason, {capacity: r.capacity, waiting: r.waiting_on_review});
}

// 354: queued-for-slot board-row presentation. A card whose role pool is
// saturated sits in phase 'dispatching' with no slot; it renders as
// "Queued for <stage>" with a real wait (from the persisted
// slot_queued_since stamp) instead of a misleading "Dispatching". Returns
// null when the card is not queued, so callers keep their ordinary label.
// Queue position is deliberately not shown: the slot manager keeps no
// ordering, so a per-card position would be invented.
function queuedPhaseLabel(sess) {
  if (!sess || sess.phase !== 'dispatching' || !sess.slot_queued_since) return null;
  return 'Queued for ' + (sess.performer_stage ? (agentLabel(agentForStage(sess.performer_stage)) || stageLabel(sess.performer_stage)) : 'performer');
}

// 146: the link to the run for the stage a card is in. Only a live session has
// one. The link sits inside clickable rows, so its clicks and keys stop there.
function viewRunLinkHtml(sess) {
  var stage = sess ? sess.performer_stage : '';
  if (!stage) return '';
  return '<a class="view-run-link" href="' + esc('/performers/' + encodeURIComponent(stage)) + '" ' +
    'aria-label="' + esc('View run: ' + stageAgentLabel(stage)) + '" ' +
    'onclick="event.stopPropagation();navigate(event,this.pathname)" onkeydown="event.stopPropagation()">View run</a>';
}

// 155: the PR checks icon and the Done lane. The functions above the markup are pure: they read
// the scoped snapshot and return data or HTML, so tests/js/board_155_checks.js can run them.

// The Done lane counts the last 24 hours, in milliseconds.
var DONE_LANE_WINDOW_MS = 86400000;
var DONE_LANE_NOTE = 'Counts merges coordinare recorded in the last 24 hours. Earlier merges are not counted.';

// The checks states a card's icon shows. The state is the gate's result at the review hand-off, not
// the PR's live state. "unknown" is a gate that could not judge the head.
var CHECKS_ICONS = {
  pass: {glyph: '&#10003;', word: 'passing'},
  fail: {glyph: '&#10007;', word: 'failing'},
  running: {glyph: '&#9679;', word: 'running'},
  blocked: {glyph: '&#9888;', word: 'blocked by environment'},
  unknown: {glyph: '?', word: 'unknown'}
};

// The server's clock when the snapshot was taken, so an age and the lane's 24-hour cut do not
// depend on this browser's clock. The browser's own clock only when the snapshot carries none.
function serverNowMs(s) {
  var at = s && s.snapshot_at ? isoMs(s.snapshot_at) : NaN;
  return isNaN(at) ? Date.now() : at;
}

// The icon beside a card's PR chip, from the card's checks view (the strip's checks). The title
// always names the hand-off and how old the result is. A stale result is neutral and says so.
function checksIconHtml(checks, nowMs) {
  if (!checks || typeof checks !== 'object') {
    var none = 'PR checks: unknown, no CI gate has judged this card yet';
    return '<span class="checks-icon checks-unknown" role="img" aria-label="' + esc(none) +
      '" title="' + esc(none) + '">?</span>';
  }
  var key = Object.prototype.hasOwnProperty.call(CHECKS_ICONS, checks.state) ? String(checks.state) : 'unknown';
  var icon = CHECKS_ICONS[key];
  // 155: nothing has read the current commit, so the gate's result is not a claim about it. Neutral, whatever it was.
  if (checks.current_checked !== true) {
    var last = 'last gate at review hand-off: ' + icon.word + '; current commit not checked';
    return '<span class="checks-icon checks-unchecked" role="img" aria-label="' + esc(last) +
      '" title="' + esc(last) + '">?</span>';
  }
  var age = fmtAgo(checks.at, nowMs);
  var title = 'PR checks at review hand-off: ' + icon.word + (age ? ' (' + age + ')' : '');
  var cls = 'checks-icon checks-' + key;
  if (checks.stale) {
    cls += ' checks-stale';
    title += ', checks as of an earlier commit';
  }
  return '<span class="' + esc(cls) + '" role="img" aria-label="' + esc(title) +
    '" title="' + esc(title) + '">' + icon.glyph + '</span>';
}

// The cards in a symphony's Done column that coordinare merged within the window, newest first.
// A card with no merge in the lifecycle log is not counted, so the lane claims no merge it did not see.
function doneLaneEntries(s, nowMs) {
  var entries = [];
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  var pipelines = s && s.card_pipeline && typeof s.card_pipeline === 'object' ? s.card_pipeline : {};
  syms.forEach(function(sym) {
    var name = sym && sym.name ? String(sym.name) : '';
    var st = sym && sym.state ? sym.state : {};
    var done = st.board_snapshot && Array.isArray(st.board_snapshot.DONE) ? st.board_snapshot.DONE : [];
    var strips = symphonyHas(pipelines, name) && pipelines[name] ? pipelines[name] : {};
    done.forEach(function(iid) {
      var strip = symphonyHas(strips, iid) ? strips[iid] : null;
      var at = strip && strip.merged_at ? isoMs(strip.merged_at) : NaN;
      if (isNaN(at) || nowMs - at > DONE_LANE_WINDOW_MS) return;
      var title = symphonyHas(st.board_titles || {}, iid) ? st.board_titles[iid] : '';
      entries.push({iid: String(iid), symphony: name, label: title || String(iid), mergedAt: strip.merged_at, at: at});
    });
  });
  entries.sort(function(a, b) { return b.at - a.at; });
  return entries;
}

// 155: the Done lane sits in the Cards heading row, so a collapsed lane adds no height to the board.
// The slot is absent on pages without the board, and then there is nothing to write.
function setDoneLane(s, nowMs) {
  var slot = document.getElementById('done-lane-slot');
  if (slot) slot.innerHTML = doneLaneHtml(doneLaneEntries(s, nowMs), nowMs);
}

// The Done lane. Collapsed by default: its summary carries the count, and the cards show when opened.
function doneLaneHtml(entries, nowMs) {
  var rows = entries.map(function(e) {
    return '<li>' + esc(e.label) + ' <span class="muted">merged ' + esc(fmtAgo(e.mergedAt, nowMs) || 'at an unknown time') + '</span></li>';
  }).join('');
  // Only the summary shows when collapsed. The note and the list sit in the opened body, so the fold holds.
  return '<details class="done-lane" id="done-lane"><summary>Done (last 24h) ' +
    '<span class="swimlane-col-count">' + entries.length + '</span></summary>' +
    '<div class="done-lane-body">' +
    '<p class="muted done-lane-note">' + esc(DONE_LANE_NOTE) + '</p>' +
    (entries.length > 0 ? '<ul class="done-lane-list">' + rows + '</ul>' : '<p class="muted">No merges in the last 24 hours.</p>') +
    '</div></details>';
}

function renderActiveWorkPanels(s) {
  var workEl = document.getElementById('swimlane-section');
  var nowMs = serverNowMs(s);
  setDoneLane(s, nowMs);
  var workCard = document.getElementById('active-work-card');
  if (!workEl) return;

  var COLUMNS = ['TODO', 'BLOCKED', 'IN_PROGRESS', 'IN_REVIEW'];
  var COLUMN_LABELS = {TODO: 'To Do', BLOCKED: 'Blocked', IN_PROGRESS: 'In Progress', IN_REVIEW: 'In Review'};

  // Index live sessions by card_id so swimlane cards can show phase/elapsed.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var sessionsByCard = {};
  sessions.forEach(function(sess) { if (sess && sess.card_id) sessionsByCard[sess.card_id] = sess; });

  // Aggregate per-card data across the symphonies in view (item_id is globally unique).
  // Each card keeps the symphony it belongs to, so a multi-symphony view can name it.
  var titles = {}, issueNums = {}, issueUrls = {}, prUrls = {};
  var symphonies = Array.isArray(s.symphonies) ? s.symphonies : [];
  var allEntries = {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []};
  var hasBoardSnapshot = false;
  var boardSnapshots = {};
  // 149: each board's To Do waits (card ids are unique across boards) and the
  // slot count each board was judged against.
  var todoWaits = {};
  var todoCapacityBySym = {};
  symphonies.forEach(function(sym) {
    var st = sym && sym.state ? sym.state : {};
    Object.assign(titles, st.board_titles || {});
    Object.assign(issueNums, st.board_issue_numbers || {});
    Object.assign(issueUrls, st.board_issue_urls || {});
    Object.assign(prUrls, st.board_pr_urls || {});
    Object.assign(todoWaits, st.todo_waits || {});
    todoCapacityBySym[sym.name || ''] = st.todo_capacity || null;
    var snap = st.board_snapshot || {};
    if (st.board_snapshot && typeof st.board_snapshot === 'object' && !Array.isArray(st.board_snapshot)) {
      hasBoardSnapshot = true;
      boardSnapshots[sym.name || ''] = st.board_snapshot;
    }
    var symName = sym.name || '';
    COLUMNS.forEach(function(col) {
      var items = Array.isArray(snap[col]) ? snap[col] : [];
      items.forEach(function(iid) {
        allEntries[col].push({iid: iid, symphony: symName});
      });
    });
  });

  // 139: the choice in the header has already narrowed the snapshot, so every card
  // here belongs to the symphonies shown. With several in view each card names its own.
  var columnItems = {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []};
  COLUMNS.forEach(function(col) { columnItems[col] = allEntries[col].slice(); });
  var multiView = symphonies.length > 1;
  var todoCapacity = symphonies.length === 1 ? (todoCapacityBySym[symphonies[0].name || ''] || null) : null;

  // 149: the snapshot the wait reasons read. The slot count is the one board's, when there is one.
  var skipAll = sessionSkipReasons(s);
  var waitSnapshot = {
    session_skip_reasons: skipAll,
    open_questions: s.open_questions || [],
    todo_waits: todoWaits,
    todo_capacity: todoCapacity,
    now: Date.now()
  };

  var totalCards = 0;
  COLUMNS.forEach(function(c) { totalCards += columnItems[c].length; });

  // Fallback when no symphony board data is available: synthesize column entries
  // from active_sessions. Each in-flight session contributes one card placed by
  // phase (blocked → BLOCKED, monitoring_pr / merging / relay_feedback → IN_REVIEW,
  // everything else → IN_PROGRESS). This keeps the swimlane usable in legacy /
  // single-symphony deployments where board_titles/board_snapshot aren't populated.
  if (!hasBoardSnapshot && totalCards === 0 && sessions.length > 0) {
    function colForPhase(ph) {
      if (ph === 'blocked') return 'BLOCKED';
      if (ph === 'monitoring_pr' || ph === 'merging' || ph === 'relay_feedback') return 'IN_REVIEW';
      return 'IN_PROGRESS';
    }
    sessions.forEach(function(sess) {
      var iid = sess && sess.card_id;
      if (!iid) return;
      titles[iid] = sess.card_title || '';
      if (sess.issue_url) issueUrls[iid] = sess.issue_url;
      if (sess.issue_number) issueNums[iid] = sess.issue_number;
      columnItems[colForPhase(sess.phase)].push({iid: iid, symphony: ''});
      totalCards++;
    });
  }

  if (totalCards === 0) {
    // 155: symphonies nobody has checked yet say why, rather than waiting for cards that may never come.
    var unchecked = symphonies.map(symphonyUncheckedNote);
    if (symphonies.length > 0 && unchecked.every(function(note) { return note !== null; })) {
      var failedCheck = unchecked.some(function(note) { return note.kind === 'failed'; });
      workEl.innerHTML = '<span class="empty-state">' + esc(symphonyUncheckedText(symphonies)) + '. ' +
        esc(failedCheck ? 'The last check did not finish.' : 'Its board has not been read yet.') + '</span>';
      if (workCard) workCard.classList.remove('card-warning');
      return;
    }
    var receivedBoards = symphonies.filter(function(sym) { return boardSnapshots[sym.name || '']; });
    if (receivedBoards.length > 0) {
      var backlogCount = 0, doneCount = 0;
      receivedBoards.forEach(function(sym) {
        var snap = boardSnapshots[sym.name || ''];
        backlogCount += Array.isArray(snap.BACKLOG) ? snap.BACKLOG.length : 0;
        doneCount += Array.isArray(snap.DONE) ? snap.DONE.length : 0;
      });
      workEl.innerHTML = '<span class="empty-state">No active cards in this symphony. Backlog: ' + backlogCount + '. Done: ' + doneCount + '.</span>';
      if (workCard) workCard.classList.remove('card-warning');
      return;
    }
    if (symphonies.length > 0) {
      workEl.innerHTML = '<span class="empty-state">Waiting for cards&hellip;</span>';
      if (workCard) workCard.classList.remove('card-warning');
      return;
    }
    var idleAndEmpty = s.phase === 'idle';
    workEl.innerHTML = idleAndEmpty
      ? '<span class="empty-state-warning">&#9888; No cards in the TODO column. Add a card to your GitHub Project with status <code>TODO</code> to start work.</span>'
      : '<span class="empty-state">Waiting for cards&hellip;</span>';
    if (workCard) workCard.classList.toggle('card-warning', idleAndEmpty);
    return;
  }
  if (workCard) workCard.classList.remove('card-warning');

  function renderCard(entry, col) {
    var iid = entry.iid;
    var title = titles[iid] || '';
    var num = issueNums[iid] || 0;
    var issueUrl = issueUrls[iid] || '';
    var prUrl = prUrls[iid] || '';
    var sess = sessionsByCard[iid];
    var pipeline = cardPipelineOf(s, iid);

    var label = num ? ('#' + num + ' ' + title) : (title || iid);
    var titleHtml = issueUrl && /^https?:\/\//i.test(issueUrl)
      ? '<a href="' + esc(issueUrl) + '" target="_blank" rel="noopener" style="text-decoration:none">' + esc(label) + ' &#8599;</a>'
      : esc(label);

    var prBadge = '';
    if (prUrl && /^https?:\/\//i.test(prUrl)) {
      prBadge = ' <a href="' + esc(prUrl) + '" target="_blank" rel="noopener" style="display:inline-block;font-size:10px;padding:1px 6px;margin-left:4px;background:var(--color-bg-pill);border:1px solid var(--color-border);border-radius:10px;text-decoration:none">PR &#8599;</a>';
      // 155: the checks state, read from the card's strip (no GitHub call). It sits beside the PR chip only.
      prBadge += ' ' + checksIconHtml(pipeline ? pipeline.checks : null, nowMs);
    }

    var liveLine = '';
    // 154: a running card's phase, stage, elapsed time and cost are on its active-run tile.
    if (sess && !isStageRunning(sess)) {
      var skip = skipAll[iid];
      // 354: a card waiting on a saturated pool is queued, not dispatching.
      var queuedLabel = queuedPhaseLabel(sess);
      // 142: a stopped session names its kind and reason, not the bare phase.
      var phaseLabel = queueLimitLabel(skip) || queuedLabel ||
        (sess.phase === 'blocked' ? esc(blockedSessionText(sess, waitSnapshot.open_questions, skipAll))
          : formatPhaseLabel(sess.phase || ''));
      var stage = sess.performer_stage ? stageAgentLabel(sess.performer_stage) : '';
      // Queued wait is measured from the queued stamp; an actively
      // dispatching card measures from when its dispatch began.
      var waitSource = queuedLabel ? sess.slot_queued_since : sess.agent_dispatch_at;
      var elapsed = waitSource ? (fmtAgo(waitSource) || '') : '';
      var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
      var elapsedHtml = elapsed
        ? (queuedLabel
          ? '<span style="color:var(--color-accent-yellow)">' + esc(elapsed) + '</span>'
          : (stale ? '<span style="color:var(--color-degraded)">⚠ ' + esc(elapsed) + '</span>' : esc(elapsed)))
        : '';
      var costStr = sess.agent_dispatch_at ? costHtml(sess, s) : 'None';
      var parts = [phaseLabel];
      if (!queuedLabel && stage) parts.push('<code>' + stage + '</code>');
      if (elapsedHtml) parts.push(elapsedHtml);
      parts.push(costStr);
      liveLine = '<div style="font-size:11px;color:var(--color-text-muted);margin-top:4px">' + parts.join(' · ') + '</div>';
    }

    var clickable = !!sess;
    var clickAttrs = clickable
      ? ' style="cursor:pointer" tabindex="0" data-card-id="' + esc(iid) + '" onclick="showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))" onkeydown="if(event.key===&quot;Enter&quot;||event.key===&quot; &quot;){showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))}"'
      : '';

    // 149: why a To Do or Blocked card is not progressing (empty for the rest).
    var waitLine = waitingLineHtml(waitingReason({id: iid, column: col, has_session: !!sess,
      block_cause: sess ? sess.block_cause : null}, waitSnapshot));

    // 139: with all symphonies in view, each card says which one it belongs to.
    var symphonyLine = multiView && entry.symphony
      ? '<div class="swimlane-card-symphony">' + esc(entry.symphony) + '</div>' : '';
    // 147: one dot per pipeline stage, the current one ringed. Read from the snapshot, no fetch.
    var pipelineDots = timelineDotsHtml(pipeline);
    // 137: a running card names its stage pair on the board, on one clamped line. Its phase and timing
    // are on its active-run tile (154), so the card does not repeat them.
    var stageLine = sess && isStageRunning(sess) && sess.performer_stage
      ? '<div class="swimlane-stage"><code>' + stageAgentLabel(sess.performer_stage) + '</code></div>' : '';
    // 155: the PR chip and its checks icon sit on their own row. Inside the two-line title clamp, a long
    // title pushes them onto a hidden third line.
    var prRow = prBadge ? '<div class="swimlane-card-pr">' + prBadge.trim() + '</div>' : '';
    return '<div class="swimlane-card"' + clickAttrs + '>' + symphonyLine +
      '<div class="swimlane-card-title" title="' + esc(title || label) + '">' + titleHtml + '</div>' +
      prRow +
      stageLine +
      pipelineDots +
      waitLine +
      liveLine +
      (isStageRunning(sess) ? '' : viewRunLinkHtml(sess)) +
      '</div>';
  }

  var html = '<div class="swimlane-grid">';
  COLUMNS.forEach(function(col) {
    var entries = columnItems[col];
    var body = entries.length === 0
      ? '<div class="swimlane-empty">None</div>'
      : entries.map(function(entry) { return renderCard(entry, col); }).join('');
    // 149: the To Do header says how full the card slots are and how many cards queue.
    var headerTitle = '';
    if (col === 'TODO') {
      var queuedCount = entries.filter(function(e) {
        return waitingReason({id: e.iid, column: 'TODO', has_session: !!sessionsByCard[e.iid]}, waitSnapshot).kind === 'queued';
      }).length;
      headerTitle = todoHeaderTitle(waitSnapshot.todo_capacity, queuedCount);
    }
    html += '<div class="swimlane-col">' +
      '<div class="swimlane-col-header"' + (headerTitle ? ' title="' + esc(headerTitle) + '"' : '') + '>' +
      esc(COLUMN_LABELS[col]) + ' <span class="swimlane-col-count">' + entries.length + '</span></div>' +
      '<div class="swimlane-col-body">' + body + '</div>' +
      '</div>';
  });
  html += '</div>';
  workEl.innerHTML = html;
}

function renderCardDetailContent(sess, s) {
  var phaseLabel = sess.phase === 'blocked'
    ? esc(blockedSessionText(sess, s.open_questions, sessionSkipReasons(s)))
    : formatPhaseLabel(sess.phase || '');
  var cardLink = sess.issue_url && /^https?:\/\//i.test(sess.issue_url)
    ? '<a href="' + esc(sess.issue_url) + '" target="_blank" rel="noopener">#' + esc(String(sess.issue_number || '')) + ' ' + esc(sess.card_title || 'unknown') + ' &#8599;</a>'
    : esc(sess.card_title || sess.card_id || 'unknown');
  var rawElapsed = sess.agent_dispatch_at ? (fmtAgo(sess.agent_dispatch_at) || 'unknown') : 'None';
  var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
  var elapsedHtml = stale
    ? '<span style="color:var(--color-degraded)">⚠ ' + esc(rawElapsed) + '</span>'
    : esc(rawElapsed);
  var cost = sess.agent_dispatch_at ? costHtml(sess, s) : 'None';
  // 348 (closes TODO(054)): logs come from the service holding this card's slot.
  var logs = Array.isArray(sess.performer_logs) ? sess.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? logs.map(function(line) { return '<div style="font-family:monospace;font-size:11px;padding:1px 0;white-space:pre-wrap;word-break:break-all">' + esc(line) + '</div>'; }).join('')
    : '<div style="color:var(--color-text-muted);font-style:italic;padding:4px 0">No log entries yet.</div>';
  // 429: the recent observer verdicts with their evidence summaries, newest
  // first. Summaries only — the coordinare never ships raw event text here,
  // so esc() is the only treatment each field needs.
  var verdicts = Array.isArray(sess.observer_verdicts) ? sess.observer_verdicts : [];
  var verdictsHtml = verdicts.length
    ? verdicts.slice().reverse().map(function(v) {
        v = v || {};
        var ev = v.evidence && typeof v.evidence === 'object' ? v.evidence : {};
        var pairs = Object.keys(ev).sort().map(function(k) { return k + '=' + String(ev[k]); }).join(', ');
        return '<div class="ov-row">' +
          '<span><span class="ov-verdict ov-' + esc(String(v.verdict || '')) + '">' + esc(String(v.verdict || '')) + '</span></span>' +
          '<span><span>' + esc(String(v.reason || '')) + '</span>' +
          '<div class="ov-meta">' + esc(pairs) + '<span class="ov-time">' + esc(String(v.at || '')) + '</span></div></span>' +
          '</div>';
      }).join('')
    : '<div style="color:var(--color-text-muted);font-style:italic;padding:4px 0">No progress checks yet.</div>';
  // 147: the pipeline tracker and lifecycle list, loaded by timelineHydrate after this HTML is set.
  var pipeline = timelinePlaceholderHtml(timelineSymphonyFor(s, sess.card_id), sess.card_id || '');
  return '<div class="card-detail-actions">' + cardActionsHtml(sess, s) + pendingControlHtml(sess) + viewRunLinkHtml(sess) + '</div>' +
    pipeline +
    '<table style="border-collapse:collapse;font-size:13px;margin-bottom:10px">' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Stage</td><td style="padding:3px 0" title="Conductor: ' + phaseLabel + '">' + (sess.performer_stage ? stageAgentLabel(sess.performer_stage) : phaseLabel) + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Card</td><td style="padding:3px 0">' + cardLink + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Run</td><td style="padding:3px 0">' + runIdHtml(sess.session_id, {copy: true}) + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Elapsed</td><td style="padding:3px 0">' + elapsedHtml + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Cost</td><td style="padding:3px 0">' + cost + '</td></tr>' +
    '</table>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em">Live Log (last 20 lines)</div>' + fullLogLinkHtml(sess) +
    '<div style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:4px;padding:8px;max-height:240px;overflow-y:auto">' + logsHtml + '</div>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin:8px 0 4px;text-transform:uppercase;letter-spacing:0.05em">Progress checks (recent)</div>' +
    '<div class="ov-list">' + verdictsHtml + '</div>' +
    // 133: the card's runs per stage, filled in by hydrateCardRuns after this HTML is set.
    '<div style="font-size:12px;color:var(--color-text-muted);margin:8px 0 4px;text-transform:uppercase;letter-spacing:0.05em">Runs (transcripts)</div>' +
    '<div class="card-runs" data-card-id="' + esc(sess.card_id || '') + '"></div>';
}

// 132: the Live Log shows the tail; the full log for this run is one click away, once the
// run has ended. The run id is the session id, the same value the log file is named after.
// 141: the link text is the run's short name; the full id is its title.
function fullLogLinkHtml(sess) {
  // Offered only once the run's log exists: it is written at teardown, not during the run.
  var runId = sess && sess.session_id ? String(sess.session_id) : '';
  if (!runId || !sess.full_log_available) return '';
  return '<div class="muted" style="margin-bottom:4px">Full log of ' +
    runIdHtml(runId, {href: '/api/runs/' + encodeURIComponent(runId) + '/log', blank: true, className: 'full-log-link'}) +
    '</div>';
}

// 141: where a run's container id is shown: the run detail's technical section only.
function containerIdHtml(containerId) {
  var full = String(containerId || '');
  if (!full) return '<span class="muted">-</span>';
  return '<span style="font-family:monospace;font-size:11px" title="' + esc(full) + '">' + esc(full.slice(0, 12)) + '</span>';
}

function showPerformerDetail(cardId) {
  _selectedCardId = cardId;
  var workSection = document.getElementById('swimlane-section');
  var detailView = document.getElementById('card-detail-view');
  if (workSection) workSection.style.display = 'none';
  if (detailView) {
    detailView.style.display = '';
    var content = document.getElementById('card-detail-content');
    // 139: the card detail reads the selected symphony's view, like every other panel.
    var scopedNow = currentScopedState();
    if (!scopedNow) {
      if (content) content.innerHTML = '<span class="empty-state">Loading&hellip;</span>';
      return;
    }
    var sessions = Array.isArray(scopedNow.active_sessions) ? scopedNow.active_sessions : [];
    var sess = sessions.find(function(s) { return s.card_id === cardId; });
    if (sess) {
      if (content) { content.innerHTML = renderCardDetailContent(sess, scopedNow); hydrateCardRuns(content); timelineHydrate(content); }
    }
  }
}

function closeCardDetail() {
  _selectedCardId = null;
  var workSection = document.getElementById('swimlane-section');
  var detailView = document.getElementById('card-detail-view');
  if (workSection) workSection.style.display = '';
  if (detailView) detailView.style.display = 'none';
}

// 154: the active-runs section. One tile per running card (layout.js), the idle
// line when none runs, and the utilization summary and last check in its header.
function renderActivePerformers(s) {
  var container = document.getElementById('active-performer-tiles');
  var summaryEl = document.getElementById('active-runs-summary');
  if (!container) return;
  if (summaryEl) summaryEl.textContent = utilizationSummaryText(s);
  // active_sessions is a list of {card_id, card_title, phase, performer_stage, ...}
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var activeSessions = sessions.filter(isStageRunning);
  if (activeSessions.length === 0) {
    container.innerHTML = activeRunsIdleHtml(s);
    return;
  }
  container.innerHTML = activeSessions.map(function(sess) { return activeRunTileHtml(sess, s); }).join('');
}

function renderDashboardExtras(s) {
  renderActivePerformers(s);
}

// 146: the run detail is addressed as /performers/<role>. The segment is client
// input: it is decoded here, checked against the live roles, and only ever
// written back through esc().
function isPerformersPath(path) {
  return path === '/performers' || path.indexOf('/performers/') === 0;
}

// The role a performers path names, or null for the list. A malformed segment
// decodes to '', which matches no role.
function performerRoleFromPath(path) {
  if (path === '/performers') return null;
  try { return decodeURIComponent(path.slice('/performers/'.length)); } catch (_malformed) { return ''; }
}

// Which view a performers path shows: 'list', 'detail' or 'not_found'.
function performerRouteView(role, roles) {
  if (role === null) return 'list';
  return role && (roles || []).indexOf(role) !== -1 ? 'detail' : 'not_found';
}

// Opening a run is a navigation, so it has its own address and Back returns to
// the list. From the keyboard, focus moves to Back so the next Tab is in the run.
function openPerformerRole(role, fromKeyboard) {
  navigate(null, '/performers/' + encodeURIComponent(role));
  if (fromKeyboard) {
    var back = document.getElementById('performers-page-back');
    if (back) back.focus();
  }
}

// Back returns to the list and puts focus on the row the run was opened from,
// so a keyboard user carries on where they were.
function hidePerformerRoleDetail() {
  var role = performerRoleFromPath(location.pathname);
  navigate(null, '/performers');
  var rows = document.querySelectorAll('#performers-page-tbody tr[data-role]');
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].getAttribute('data-role') === role) { rows[i].focus(); return; }
  }
}

// 049 + 053: Performers page list + drilldown detail
function renderPerformersPage(s) {
  var tbody = document.getElementById('performers-page-tbody');
  if (!tbody) return;
  var util = s.role_utilization || [];
  var listView = document.getElementById('performers-page-list-view');
  var detailView = document.getElementById('performers-page-detail-view');
  var detailEl = document.getElementById('performers-page-detail');

  // active_sessions is a list of {card_id, card_title, phase, performer_stage, ...}
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var activeByStage = {};
  sessions.forEach(function(sess) {
    var stage = sess.performer_stage || '';
    var phase = sess.phase || '';
    if ((phase === 'monitoring_performer' || phase === 'monitoring_agent') && stage) {
      activeByStage[stage] = activeByStage[stage] || [];
      activeByStage[stage].push(sess);
    }
  });
  var utilByRole = {};
  util.forEach(function(r) { utilByRole[r.role] = r; });
  // 146: the view comes from the address, so Back and the browser's buttons agree.
  var rawRole = performerRoleFromPath(location.pathname);
  var view = performerRouteView(rawRole, util.map(function(r) { return r.role; }));
  var selectedRole = view === 'detail' ? rawRole : null;

  tbody.innerHTML = util.length === 0
    ? '<tr><td colspan="5" class="empty-state">No performer roles configured</td></tr>'
    : util.map(function(r) {
    var isActive = r.active > 0;
    var isSelected = selectedRole === r.role;
    var badge = '<span class="role-status-badge ' + (isActive ? 'role-active' : 'role-idle') + '">' + (isActive ? 'active' : 'idle') + '</span>';
    var cards = (activeByStage[r.role] || []).map(function(sess) {
      return esc((sess.card_title || sess.card_id || '').substring(0, 40));
    }).join('<br>') || '<span class="empty-state">None</span>';
    var maxVal = r.max > 0 ? r.max : 1;
    var activeVal = isActive ? (r.active || 1) : 0;
    var utilStr = utilizationBarHtml(r) + ' ' + esc(busyText(activeVal, maxVal, 0));
    var queued = r.queued > 0 ? ' <span style="color:var(--color-accent-yellow)">· ' + r.queued + ' queued</span>' : '';
    return '<tr class="performers-row' + (isSelected ? ' row-selected' : '') + '" data-role="' + esc(r.role) + '" tabindex="0" role="button" aria-label="Open details for ' + esc(r.role) + '">' +
      '<td style="font-weight:bold">' + roleOrStageLabel(r.role) + '</td><td>' + badge + '</td><td style="font-size:12px">' + cards + '</td><td>' + utilStr + queued + '</td><td class="row-chevron" aria-hidden="true"></td></tr>';
  }).join('');

  tbody.querySelectorAll('tr[data-role]').forEach(function(row) {
    row.addEventListener('click', function() {
      openPerformerRole(row.getAttribute('data-role') || '', false);
    });
    row.addEventListener('keydown', function(event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openPerformerRole(row.getAttribute('data-role') || '', true);
      }
    });
  });

  if (!listView || !detailView || !detailEl) return;
  if (view === 'list') {
    listView.style.display = '';
    detailView.style.display = 'none';
    return;
  }
  listView.style.display = 'none';
  detailView.style.display = '';
  if (view === 'not_found') {
    detailEl.innerHTML = '<div><strong>Run not found</strong></div>' +
      '<div class="muted" style="margin-top:4px">No performer role matches this address. Pick one from the list.</div>';
    return;
  }

  var row = utilByRole[selectedRole] || {role: selectedRole, active: 0, max: 0, queued: 0};
  var selectedSessions = activeByStage[selectedRole] || [];
  var isSelectedRoleActive = selectedSessions.length > 0;
  var statusBadge = '<span class="role-status-badge ' + (isSelectedRoleActive ? 'role-active' : 'role-idle') + ' detail-badge">' + (isSelectedRoleActive ? 'active' : 'idle') + '</span>';
  var roleCardsHtml = selectedSessions.length
    ? '<ul class="detail-list">' + selectedSessions.map(function(sess) {
      return '<li>' + esc(sess.card_title || sess.card_id || '-') + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No run in progress for this role.</div>';
  // 348: the panels below describe ONE card -- name it.
  var telemetryFor = selectedSessions.length > 1
    ? '<div class="muted" style="margin-top:4px;font-size:12px">Live panels below show <strong>' + esc((selectedSessions[0] || {}).card_title || (selectedSessions[0] || {}).card_id || 'unknown') + '</strong> (' + String(selectedSessions.length) + ' cards on this role).</div>'
    : '';
  // 348: telemetry is per-card, so read the session running this role rather
  // than top-level state (null in shared-pool mode). No fallback: a role with
  // no live session genuinely has no data.
  var sel = selectedSessions[0] || {};
  var sessionStats = sel.session_stats;
  var statsLine = sessionStats
    ? esc((sessionStats.title || '') + (sessionStats.title ? ' · ' : '') + sessionStats.files_changed + ' files · +' + sessionStats.lines_added + '/-' + sessionStats.lines_removed + ' lines')
    : 'No run stats available yet.';
  var metrics = sel.performer_metrics || {};
  // 134: this run's tokens, with the breakdown on hover. The status counter is the fallback.
  var runUsage = usageSummaryOf(sel, 'run');
  var fallbackTokens = derivePerformerTokenTotal(sel);
  var tokenTotal = usageTotalOf(runUsage, fallbackTokens);
  // 122: never a bare 0 while the stage is still running.
  var tokenView = tokenReadout(tokenTotal, isStageRunning(sel));
  var usageView = usageTokensReadout(runUsage, fallbackTokens);
  var tokenText = tokenView.pending
    ? esc(tokenView.text) + ' (' + esc(USAGE_PENDING_NOTE.toLowerCase()) + ')'
    : '<span title="' + esc(usageView.title) + '">' + esc(usageView.text) + '</span>';
  var metricsLine = (metrics && metrics.pid != null)
    ? (
      'PID ' + String(metrics.pid) +
      ' · CPU ' + esc(metrics.cpu_percent != null ? metrics.cpu_percent.toFixed(1) + '%' : 'unknown') +
      ' · Memory ' + esc(fmtBytes(metrics.memory_bytes)) +
      ' · Tokens ' + tokenText
    )
    : (tokenTotal != null || tokenView.pending
        ? 'Tokens ' + tokenText + ' · no process metrics (containerised performer)'
        : 'No live metrics available yet.');
  var events = Array.isArray(sel.performer_events) ? sel.performer_events.slice(-12) : [];
  var eventsHtml = events.length
    ? '<ul class="detail-list">' + events.map(function(ev) {
      return '<li><strong>' + esc(ev.type || 'event') + ':</strong> ' + esc(ev.text || '') + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No live events yet.</div>';
  var logs = Array.isArray(sel.performer_logs) ? sel.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? '<div class="detail-log">' + logs.map(function(line) { return '<div>' + esc(line) + '</div>'; }).join('') + '</div>'
    : '<div class="muted">No stderr logs yet.</div>';

  var backendLine = esc(sel.performer_backend || 'performer');
  if (sel.backend_ui_url) {
    try {
      var parsed = new URL(sel.backend_ui_url);
      if (parsed.protocol === 'http:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost')) {
        backendLine += ' &middot; <a href="' + esc(parsed.href) + '" target="_blank" rel="noopener noreferrer">Open in browser &#8599;</a>';
      }
    } catch (_ignore) {}
  }

  // Build skip-reason diagnostics for sessions associated with this role only.
  var skipReasons = sessionSkipReasons(s);
  var roleSkips = [];
  selectedSessions.forEach(function(sess) {
    var reason = skipReasons[sess.card_id];
    if (reason) roleSkips.push({card: sess.card_title || sess.card_id, reason: reason});
  });
  var skipHtml = roleSkips.length
    ? '<ul class="detail-list">' + roleSkips.map(function(sr) {
        // 142: the wait in words the Blocked column uses ("Waiting on #N"), never the wire code.
        var label = esc(skipReasonText(sr.reason));
        return '<li><span style="color:var(--color-accent-orange)">' + esc(sr.card) + '</span>: ' + label + '</li>';
      }).join('') + '</ul>'
    : '';

  detailEl.innerHTML =
    '<div><strong>' + roleOrStageLabel(selectedRole) + '</strong>' + statusBadge + '</div>' +
    '<div class="muted" style="margin-top:4px">' + esc(busyText(row.active, row.max, row.queued)) + '</div>' +
    '<div class="detail-block"><strong>Card Context</strong>' + roleCardsHtml + telemetryFor + stepTrailHtml(sel, s.stall_timeout_seconds) + (skipHtml ? '<div style="margin-top:8px"><span style="color:var(--color-accent-yellow);font-size:12px">Skipped this cycle:</span>' + skipHtml + '</div>' : '') + '</div>' +
    // 141: the run's name links to its transcript (the run id is the session id). Copy sits beside it.
    '<div class="detail-block"><strong>Run</strong><div class="muted" style="margin-top:4px">' +
      runIdHtml(sel.session_id || s.agent_session_id, {
        href: RUN_NAME_PATTERN.test(String(sel.session_id || '')) ? '/runs/' + encodeURIComponent(String(sel.session_id)) : '',
        copy: true,
      }) +
      ' &middot; Dispatched ' + timeHtml(sel.agent_dispatch_at || s.agent_dispatch_at, '-') +
      '</div><div class="muted" style="margin-top:4px">' + backendLine + '</div><div class="muted" style="margin-top:4px">' + statsLine + '</div></div>' +
    '<div class="detail-block"><strong>Metrics</strong><div class="muted" style="margin-top:4px">' + metricsLine + '</div></div>' +
    '<div class="detail-block"><strong>Technical</strong><div class="muted" style="margin-top:4px">Container: ' + containerIdHtml(sel.container_id) + '</div></div>' +
    '<div class="detail-block"><strong>Live Events</strong>' + eventsHtml + '</div>' +
    '<div class="detail-block"><strong>Process Logs (stderr)</strong>' + logsHtml + '</div>';
}
