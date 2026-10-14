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

function utilizationRowHtml(r) {
  var max = Number(r.max) || 0;
  var active = Number(r.active) || 0;
  var queued = Number(r.queued) || 0;
  var pct = max > 0 ? Math.min(100, Math.round(active / max * 100)) : 0;
  var bar = '<div style="background:var(--color-bar-track);border-radius:3px;height:8px;width:60px;display:inline-block;vertical-align:middle"><div style="background:' + utilizationBarColor(r) + ';height:100%;width:' + pct + '%;border-radius:3px"></div></div>';
  var queuedHtml = queued > 0
    ? ' <span class="util-queued" style="color:var(--color-accent-yellow)" title="Cards waiting for a free ' + roleOrStageLabel(r.role) + ' slot">· ' + queued + ' queued</span>'
    : '';
  return '<tr><td>' + roleOrStageLabel(r.role) + '</td><td>' + bar + ' ' + active + ' of ' + max + ' busy' + queuedHtml + '</td></tr>';
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
  var html = '<span class="ap-pill ap-pill-card" title="' + esc(nodeId ? 'Board item ' + nodeId : '') + '"><strong>' + inner + '</strong></span>';
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
  if (!iso) return '<strong>—</strong>';
  return '<strong data-elapsed-since="' + esc(iso) + '" title="Time since this stage run was dispatched">' + esc(fmtAge(iso) || '—') + '</strong>';
}

function refreshElapsedReadouts(root) {
  var nodes = (root || document).querySelectorAll('[data-elapsed-since]');
  for (var i = 0; i < nodes.length; i++) {
    var prefix = nodes[i].getAttribute('data-elapsed-prefix') || '';
    var suffix = nodes[i].getAttribute('data-elapsed-suffix') || '';
    nodes[i].textContent = prefix + (fmtAge(nodes[i].getAttribute('data-elapsed-since')) || '—') + suffix;
  }
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
  if (running) return { text: '—', title: USAGE_PENDING_NOTE, pending: true };
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

// in-flight work the coordinare is reasoning about.
var _swimlaneTabExplicit = false;
// 146: the link to the run for the stage a card is in. Only a live session has
// one. The link sits inside clickable rows, so its clicks and keys stop there.
function viewRunLinkHtml(sess) {
  var stage = sess ? sess.performer_stage : '';
  if (!stage) return '';
  return '<a class="view-run-link" href="' + esc('/performers/' + encodeURIComponent(stage)) + '" ' +
    'aria-label="' + esc('View run: ' + stageAgentLabel(stage)) + '" ' +
    'onclick="event.stopPropagation();navigate(event,this.pathname)" onkeydown="event.stopPropagation()">View run</a>';
}

function renderActiveWorkPanels(s) {
  var workEl = document.getElementById('swimlane-section');
  var workCard = document.getElementById('active-work-card');
  var tabsEl = document.getElementById('swimlane-tabs');
  if (!workEl) return;

  var COLUMNS = ['TODO', 'BLOCKED', 'IN_PROGRESS', 'IN_REVIEW'];
  var COLUMN_LABELS = {TODO: 'To Do', BLOCKED: 'Blocked', IN_PROGRESS: 'In Progress', IN_REVIEW: 'In Review'};

  // Index live sessions by card_id so swimlane cards can show phase/elapsed.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var sessionsByCard = {};
  sessions.forEach(function(sess) { if (sess && sess.card_id) sessionsByCard[sess.card_id] = sess; });

  // Aggregate per-card data across all symphonies (item_id is globally unique).
  // Track which symphony each card belongs to for tab filtering.
  var titles = {}, issueNums = {}, issueUrls = {}, prUrls = {};
  var symphonies = Array.isArray(s.symphonies) ? s.symphonies : [];
  var perSymCounts = {};  // symphony name → total card count
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
    perSymCounts[symName] = 0;
    COLUMNS.forEach(function(col) {
      var items = Array.isArray(snap[col]) ? snap[col] : [];
      items.forEach(function(iid) {
        allEntries[col].push({iid: iid, symphony: symName});
        perSymCounts[symName] += 1;
      });
    });
  });

  // Prefer a received board until the operator explicitly chooses a tab.
  var symphonyNames = symphonies.map(function(sym) { return sym.name || ''; }).filter(Boolean);
  if (symphonyNames.length > 0) {
    if (!_swimlaneTab || symphonyNames.indexOf(_swimlaneTab) === -1) {
      _swimlaneTabExplicit = false;
      _swimlaneTab = symphonyNames.find(function(name) { return boardSnapshots[name]; }) || symphonyNames[0];
    } else if (!_swimlaneTabExplicit && !boardSnapshots[_swimlaneTab]) {
      _swimlaneTab = symphonyNames.find(function(name) { return boardSnapshots[name]; }) || _swimlaneTab;
    }
  } else {
    _swimlaneTab = null;
    _swimlaneTabExplicit = false;
  }

  // Filter to the active tab (only one symphony's cards visible at a time).
  var columnItems = {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []};
  COLUMNS.forEach(function(col) {
    columnItems[col] = _swimlaneTab
      ? allEntries[col].filter(function(e) { return e.symphony === _swimlaneTab; })
      : allEntries[col].slice();
  });

  // Render tab bar when there are 2+ symphonies; hide otherwise.
  if (tabsEl) {
    if (symphonyNames.length > 1) {
      tabsEl.style.display = '';
      tabsEl.innerHTML = symphonyNames.map(function(name) {
        var cls = 'swimlane-tab' + (name === _swimlaneTab ? ' active' : '');
        var count = perSymCounts[name] || 0;
        return '<button type="button" class="' + cls + '" data-symphony="' + esc(name) + '" onclick="selectSwimlaneTab(this.getAttribute(&quot;data-symphony&quot;))">' +
          esc(name) + '<span class="swimlane-tab-count">' + count + '</span></button>';
      }).join('');
    } else {
      tabsEl.style.display = 'none';
      tabsEl.innerHTML = '';
    }
  }

  // 149: the snapshot the wait reasons read. The slot count is the selected board's.
  var skipAll = sessionSkipReasons(s);
  var waitSnapshot = {
    session_skip_reasons: skipAll,
    open_questions: s.open_questions || [],
    todo_waits: todoWaits,
    todo_capacity: _swimlaneTab ? (todoCapacityBySym[_swimlaneTab] || null) : null,
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
    var selectedSnapshot = _swimlaneTab ? boardSnapshots[_swimlaneTab] : null;
    if (selectedSnapshot) {
      var backlogCount = Array.isArray(selectedSnapshot.BACKLOG) ? selectedSnapshot.BACKLOG.length : 0;
      var doneCount = Array.isArray(selectedSnapshot.DONE) ? selectedSnapshot.DONE.length : 0;
      workEl.innerHTML = '<span class="empty-state">No active cards in this board. Backlog: ' + backlogCount + '. Done: ' + doneCount + '.</span>';
      if (workCard) workCard.classList.remove('card-warning');
      return;
    }
    if (_swimlaneTab) {
      workEl.innerHTML = '<span class="empty-state">Waiting for board snapshot&hellip;</span>';
      if (workCard) workCard.classList.remove('card-warning');
      return;
    }
    var idleAndEmpty = s.phase === 'idle';
    workEl.innerHTML = idleAndEmpty
      ? '<span class="empty-state-warning">&#9888; No cards in the TODO column &mdash; add a card to your GitHub Project board with status <code>TODO</code> to start work.</span>'
      : '<span class="empty-state">Waiting for board snapshot&hellip;</span>';
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

    var label = num ? ('#' + num + ' ' + title) : (title || iid);
    var titleHtml = issueUrl && /^https?:\/\//i.test(issueUrl)
      ? '<a href="' + esc(issueUrl) + '" target="_blank" rel="noopener" style="text-decoration:none">' + esc(label) + ' &#8599;</a>'
      : esc(label);

    var prBadge = '';
    if (prUrl && /^https?:\/\//i.test(prUrl)) {
      prBadge = ' <a href="' + esc(prUrl) + '" target="_blank" rel="noopener" style="display:inline-block;font-size:10px;padding:1px 6px;margin-left:4px;background:var(--color-bg-pill);border:1px solid var(--color-border);border-radius:10px;text-decoration:none">PR &#8599;</a>';
    }

    var liveLine = '';
    if (sess) {
      var skip = skipAll[iid];
      // 354: a card waiting on a saturated pool is queued, not dispatching.
      var queuedLabel = queuedPhaseLabel(sess);
      var phaseLabel = queueLimitLabel(skip) || queuedLabel || formatPhaseLabel(sess.phase || '');
      var stage = sess.performer_stage ? stageAgentLabel(sess.performer_stage) : '';
      // Queued wait is measured from the queued stamp; an actively
      // dispatching card measures from when its dispatch began.
      var waitSource = queuedLabel ? sess.slot_queued_since : sess.agent_dispatch_at;
      var elapsed = waitSource ? (fmtAge(waitSource) || '') : '';
      var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
      var elapsedHtml = elapsed
        ? (queuedLabel
          ? '<span style="color:var(--color-accent-yellow)">' + esc(elapsed) + '</span>'
          : (stale ? '<span style="color:var(--color-degraded)">⚠ ' + esc(elapsed) + '</span>' : esc(elapsed)))
        : '';
      var costStr = sess.agent_dispatch_at ? costHtml(sess, s) : '—';
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
    var waitLine = waitingLineHtml(waitingReason({id: iid, column: col, has_session: !!sess}, waitSnapshot));

    return '<div class="swimlane-card"' + clickAttrs + '>' +
      '<div style="font-size:13px;line-height:1.35">' + titleHtml + prBadge + '</div>' +
      waitLine +
      liveLine +
      viewRunLinkHtml(sess) +
      '</div>';
  }

  var html = '<div class="swimlane-grid">';
  COLUMNS.forEach(function(col) {
    var entries = columnItems[col];
    var body = entries.length === 0
      ? '<div class="swimlane-empty">&mdash;</div>'
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
  var phaseLabel = formatPhaseLabel(sess.phase || '');
  var cardLink = sess.issue_url && /^https?:\/\//i.test(sess.issue_url)
    ? '<a href="' + esc(sess.issue_url) + '" target="_blank" rel="noopener">#' + esc(String(sess.issue_number || '')) + ' ' + esc(sess.card_title || '—') + ' &#8599;</a>'
    : esc(sess.card_title || sess.card_id || '—');
  var rawElapsed = sess.agent_dispatch_at ? (fmtAge(sess.agent_dispatch_at) || '—') : '—';
  var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
  var elapsedHtml = stale
    ? '<span style="color:var(--color-degraded)">⚠ ' + esc(rawElapsed) + '</span>'
    : esc(rawElapsed);
  var cost = sess.agent_dispatch_at ? costHtml(sess, s) : '—';
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
  return '<div class="card-detail-actions">' + cardActionsHtml(sess, s) + pendingControlHtml(sess) + viewRunLinkHtml(sess) + '</div>' +
    '<table style="border-collapse:collapse;font-size:13px;margin-bottom:10px">' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Stage</td><td style="padding:3px 0" title="Conductor: ' + phaseLabel + '">' + (sess.performer_stage ? stageAgentLabel(sess.performer_stage) : phaseLabel) + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Card</td><td style="padding:3px 0">' + cardLink + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Elapsed</td><td style="padding:3px 0">' + elapsedHtml + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Cost</td><td style="padding:3px 0">' + cost + '</td></tr>' +
    '</table>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em">Live Log (last 20 lines)</div>' + fullLogLinkHtml(sess) +
    '<div style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:4px;padding:8px;max-height:240px;overflow-y:auto">' + logsHtml + '</div>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin:8px 0 4px;text-transform:uppercase;letter-spacing:0.05em">Progress checks (recent)</div>' +
    '<div class="ov-list">' + verdictsHtml + '</div>';
}

// 132: the Live Log shows the tail; the full log for this run is one click away. The
// run id is the session id, the same value the run's log file is named after.
function fullLogLinkHtml(sess) {
  var runId = sess && sess.session_id ? String(sess.session_id) : '';
  if (!runId) return '';
  return '<a class="full-log-link" href="' + esc('/api/runs/' + encodeURIComponent(runId) + '/log') + '" ' +
    'target="_blank" rel="noopener">Open full log &#8599;</a>';
}

function showPerformerDetail(cardId) {
  _selectedCardId = cardId;
  var workSection = document.getElementById('swimlane-section');
  var detailView = document.getElementById('card-detail-view');
  if (workSection) workSection.style.display = 'none';
  if (detailView) {
    detailView.style.display = '';
    var content = document.getElementById('card-detail-content');
    if (!_lastState) {
      if (content) content.innerHTML = '<span class="empty-state">Loading&hellip;</span>';
      return;
    }
    var sessions = Array.isArray(_lastState.active_sessions) ? _lastState.active_sessions : [];
    var sess = sessions.find(function(s) { return s.card_id === cardId; });
    if (sess) {
      if (content) content.innerHTML = renderCardDetailContent(sess, _lastState);
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

function renderActivePerformers(s) {
  var container = document.getElementById('active-performer-tiles');
  var section = document.getElementById('active-performers');
  if (!container || !section) return;
  // active_sessions is a list of {card_id, card_title, phase, performer_stage, ...}
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var activeSessions = sessions.filter(function(sess) {
    return sess.phase === 'monitoring_performer' || sess.phase === 'monitoring_agent';
  });
  if (activeSessions.length === 0) {
    section.style.display = '';
    var phase = s.phase_label || s.phase || 'idle';
    var cycleCount = s.cycles_completed != null ? s.cycles_completed : '—';
    var summary = (s.board_summary && typeof s.board_summary === 'object') ? s.board_summary : {};
    function count(key) {
      var value = summary[key];
      return Number.isFinite(value) ? value : 0;
    }
    var pollHint = s.last_poll_at ? esc(fmtTime(s.last_poll_at)) : '—';
    var filterHint = s.ownership_hint ? esc(s.ownership_hint) : '';
    container.innerHTML =
      '<div class="ap-idle">' +
        '<div class="ap-idle-title">No active performers</div>' +
        '<div class="ap-idle-rows">' +
          '<div class="ap-idle-row">' +
            '<span class="ap-pill">Status: <strong>' + esc(phase) + '</strong></span>' +
            '<span class="ap-pill">Checks: <strong>' + esc(String(cycleCount)) + '</strong></span>' +
            '<span class="ap-pill">Last checked: <strong>' + pollHint + '</strong></span>' +
            (filterHint ? '<span class="ap-pill">Filter: <strong>' + filterHint + '</strong></span>' : '') +
          '</div>' +
          '<div class="ap-idle-row">' +
            '<span class="ap-pill">TODO <strong>' + String(count('TODO')) + '</strong></span>' +
            '<span class="ap-pill">IN_PROGRESS <strong>' + String(count('IN_PROGRESS')) + '</strong></span>' +
            '<span class="ap-pill">IN_REVIEW <strong>' + String(count('IN_REVIEW')) + '</strong></span>' +
            '<span class="ap-pill">DONE <strong>' + String(count('DONE')) + '</strong></span>' +
          '</div>' +
        '</div>' +
      '</div>';
    return;
  }
  section.style.display = '';
  var html = '';
  activeSessions.forEach(function(sess) {
    var stage = sess.performer_stage ? stageAgentLabel(sess.performer_stage) : '—';
    var title = sess.card_title || sess.card_id || '—';
    var dispatchAt = sess.agent_dispatch_at || s.agent_dispatch_at;
    var phaseLabel = formatPhaseLabel(sess.phase || 'active');
    html += '<div class="ap-tile">' +
      '<div class="ap-tile-header">' +
        '<div class="ap-tile-role"><span class="perf-dot perf-running"></span>' + stage + '</div>' +
        '<div class="ap-tile-phase">' + phaseLabel + '</div>' +
        cardActionsHtml(sess, s) +
        viewRunLinkHtml(sess) +
      '</div>' +
      '<div class="ap-tile-title">' + esc(title) + '</div>' +
      '<div class="ap-tile-meta">' +
        '<span class="ap-pill ap-tile-elapsed">&#9201; ' + elapsedSinceHtml(dispatchAt) + '</span>' +
        cardRefHtml(sess) +
        pendingControlHtml(sess) +
        // 138 FR-032: quiet marker — observation only, never a phase change.
        // .af-kind carries no colours of its own, so .ev-quiet is not overridden
        // by .ap-pill (equal specificity, later in the sheet).
        (_afQuietCards[sess.card_id] ? '<span class="af-kind ev-quiet">QUIET</span>' : '') +
      '</div>' +
      '</div>';
  });
  container.innerHTML = html;
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
    }).join('<br>') || '<span class="empty-state">—</span>';
    var maxVal = r.max > 0 ? r.max : 1;
    var activeVal = isActive ? (r.active || 1) : 0;
    var utilStr = esc(busyText(activeVal, maxVal, 0));
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
      var cid = sess.container_id ? ' <span class="muted" style="font-family:monospace;font-size:11px">[' + esc(sess.container_id.slice(0, 12)) + ']</span>' : '';
      return '<li>' + esc(sess.card_title || sess.card_id || '—') + cid + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No active session currently running for this role.</div>';
  // 348: the panels below describe ONE card -- name it.
  var telemetryFor = selectedSessions.length > 1
    ? '<div class="muted" style="margin-top:4px;font-size:12px">Live panels below show <strong>' + esc((selectedSessions[0] || {}).card_title || (selectedSessions[0] || {}).card_id || '—') + '</strong> (' + String(selectedSessions.length) + ' cards on this role).</div>'
    : '';
  // 348: telemetry is per-card, so read the session running this role rather
  // than top-level state (null in shared-pool mode). No fallback: a role with
  // no live session genuinely has no data.
  var sel = selectedSessions[0] || {};
  var sessionStats = sel.session_stats;
  var statsLine = sessionStats
    ? esc((sessionStats.title || '') + (sessionStats.title ? ' · ' : '') + sessionStats.files_changed + ' files · +' + sessionStats.lines_added + '/-' + sessionStats.lines_removed + ' lines')
    : 'No session stats available yet.';
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
      ' · CPU ' + esc(metrics.cpu_percent != null ? metrics.cpu_percent.toFixed(1) + '%' : '—') +
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
        var r = sr.reason || {};
        var label = esc(queueLimitLabel(r) || r.reason || 'skipped');
        var blockers = Array.isArray(r.blockers) && r.blockers.length
          ? ' (blocked by #' + r.blockers.map(function(n) { return esc(String(n)); }).join(', #') + ')' : '';
        return '<li><span style="color:var(--color-accent-orange)">' + esc(sr.card) + '</span> — ' + label + blockers + '</li>';
      }).join('') + '</ul>'
    : '';

  detailEl.innerHTML =
    '<div><strong>' + roleOrStageLabel(selectedRole) + '</strong>' + statusBadge + '</div>' +
    '<div class="muted" style="margin-top:4px">' + esc(busyText(row.active, row.max, row.queued)) + '</div>' +
    '<div class="detail-block"><strong>Card Context</strong>' + roleCardsHtml + telemetryFor + stepTrailHtml(sel, s.stall_timeout_seconds) + (skipHtml ? '<div style="margin-top:8px"><span style="color:var(--color-accent-yellow);font-size:12px">Skipped this cycle:</span>' + skipHtml + '</div>' : '') + '</div>' +
    '<div class="detail-block"><strong>Session</strong><div class="muted" style="margin-top:4px">Session: ' + esc(sel.session_id || s.agent_session_id || '—') + ' &middot; Uptime: ' + esc(fmtAge(sel.agent_dispatch_at || s.agent_dispatch_at) || '—') + '</div><div class="muted" style="margin-top:4px">' + backendLine + '</div><div class="muted" style="margin-top:4px">' + statsLine + '</div></div>' +
    '<div class="detail-block"><strong>Metrics</strong><div class="muted" style="margin-top:4px">' + metricsLine + '</div></div>' +
    '<div class="detail-block"><strong>Live Events</strong>' + eventsHtml + '</div>' +
    '<div class="detail-block"><strong>Process Logs (stderr)</strong>' + logsHtml + '</div>';
}
