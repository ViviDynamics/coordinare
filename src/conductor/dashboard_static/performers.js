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

// 354: queued-for-slot board-row presentation. A card whose role pool is
// saturated sits in phase 'dispatching' with no slot; it renders as
// "Queued for <stage>" with a real wait (from the persisted
// slot_queued_since stamp) instead of a misleading "Dispatching". Returns
// null when the card is not queued, so callers keep their ordinary label.
// Queue position is deliberately not shown: the slot manager keeps no
// ordering, so a per-card position would be invented.
function queuedPhaseLabel(sess) {
  if (!sess || sess.phase !== 'dispatching' || !sess.slot_queued_since) return null;
  return 'Queued for ' + (sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : 'performer');
}

// in-flight work the coordinare is reasoning about.
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
  symphonies.forEach(function(sym) {
    var st = sym && sym.state ? sym.state : {};
    Object.assign(titles, st.board_titles || {});
    Object.assign(issueNums, st.board_issue_numbers || {});
    Object.assign(issueUrls, st.board_issue_urls || {});
    Object.assign(prUrls, st.board_pr_urls || {});
    var snap = st.board_snapshot || {};
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

  // Determine which symphony tab is active (default: first symphony).
  var symphonyNames = symphonies.map(function(sym) { return sym.name || ''; }).filter(Boolean);
  if (symphonyNames.length > 0) {
    if (!_swimlaneTab || symphonyNames.indexOf(_swimlaneTab) === -1) {
      _swimlaneTab = symphonyNames[0];
    }
  } else {
    _swimlaneTab = null;
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

  var totalCards = 0;
  COLUMNS.forEach(function(c) { totalCards += columnItems[c].length; });

  // Fallback when no symphony board data is available: synthesize column entries
  // from active_sessions. Each in-flight session contributes one card placed by
  // phase (blocked → BLOCKED, monitoring_pr / merging / relay_feedback → IN_REVIEW,
  // everything else → IN_PROGRESS). This keeps the swimlane usable in legacy /
  // single-symphony deployments where board_titles/board_snapshot aren't populated.
  if (totalCards === 0 && sessions.length > 0) {
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
    var idleAndEmpty = s.phase === 'idle';
    workEl.innerHTML = idleAndEmpty
      ? '<span class="empty-state-warning">&#9888; No cards in the TODO column &mdash; add a card to your GitHub Project board with status <code>TODO</code> to start work.</span>'
      : '<span class="empty-state">Waiting for board snapshot&hellip;</span>';
    if (workCard) workCard.classList.toggle('card-warning', idleAndEmpty);
    return;
  }
  if (workCard) workCard.classList.remove('card-warning');

  function renderCard(entry) {
    var iid = entry.iid;
    var title = titles[iid] || '';
    var num = issueNums[iid] || 0;
    var issueUrl = issueUrls[iid] || '';
    var prUrl = prUrls[iid] || '';
    var sess = sessionsByCard[iid];

    var label = num ? ('#' + num + ' ' + title) : (title || iid);
    var titleHtml = issueUrl && /^https?:\/\//i.test(issueUrl)
      ? '<a href="' + esc(issueUrl) + '" target="_blank" rel="noopener" style="color:var(--color-accent-blue);text-decoration:none">' + esc(label) + ' &#8599;</a>'
      : esc(label);

    var prBadge = '';
    if (prUrl && /^https?:\/\//i.test(prUrl)) {
      prBadge = ' <a href="' + esc(prUrl) + '" target="_blank" rel="noopener" style="display:inline-block;font-size:10px;padding:1px 6px;margin-left:4px;background:var(--color-bg-pill);border:1px solid var(--color-border);border-radius:10px;color:var(--color-accent-blue);text-decoration:none">PR &#8599;</a>';
    }

    var liveLine = '';
    if (sess) {
      var skip = (s.session_skip_reasons || {})[iid];
      // 354: a card waiting on a saturated pool is queued, not dispatching.
      var queuedLabel = queuedPhaseLabel(sess);
      var phaseLabel = skip && skip.reason === 'pipeline_capacity'
        ? 'Queued — issue limit reached' : (queuedLabel || formatPhaseLabel(sess.phase || ''));
      var stage = sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : '';
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
      var costStr = sess.agent_dispatch_at
        ? '$' + (Number(sess.card_cost_estimate) || 0).toFixed(4)
        : '—';
      var parts = [phaseLabel];
      if (!queuedLabel && stage) parts.push('<code>' + stage + '</code>');
      if (elapsedHtml) parts.push(elapsedHtml);
      parts.push(esc(costStr));
      liveLine = '<div style="font-size:11px;color:var(--color-text-muted);margin-top:4px">' + parts.join(' · ') + '</div>';
    }

    var clickable = !!sess;
    var clickAttrs = clickable
      ? ' style="cursor:pointer" tabindex="0" data-card-id="' + esc(iid) + '" onclick="showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))" onkeydown="if(event.key===&quot;Enter&quot;||event.key===&quot; &quot;){showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))}"'
      : '';

    return '<div class="swimlane-card"' + clickAttrs + '>' +
      '<div style="font-size:13px;line-height:1.35">' + titleHtml + prBadge + '</div>' +
      liveLine +
      '</div>';
  }

  var html = '<div class="swimlane-grid">';
  COLUMNS.forEach(function(col) {
    var entries = columnItems[col];
    var body = entries.length === 0
      ? '<div class="swimlane-empty">&mdash;</div>'
      : entries.map(renderCard).join('');
    html += '<div class="swimlane-col">' +
      '<div class="swimlane-col-header">' + esc(COLUMN_LABELS[col]) + ' <span class="swimlane-col-count">' + entries.length + '</span></div>' +
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
  var cost = sess.agent_dispatch_at
    ? '$' + (sess.card_cost_estimate || 0).toFixed(4) : '—';
  // 348 (closes TODO(054)): logs come from the service holding this card's slot.
  var logs = Array.isArray(sess.performer_logs) ? sess.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? logs.map(function(line) { return '<div style="font-family:monospace;font-size:11px;padding:1px 0;white-space:pre-wrap;word-break:break-all">' + esc(line) + '</div>'; }).join('')
    : '<div style="color:var(--color-text-muted);font-style:italic;padding:4px 0">No log entries yet.</div>';
  return '<table style="border-collapse:collapse;font-size:13px;margin-bottom:10px">' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Phase</td><td style="padding:3px 0">' + phaseLabel + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Card</td><td style="padding:3px 0">' + cardLink + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Elapsed</td><td style="padding:3px 0">' + elapsedHtml + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Cost</td><td style="padding:3px 0">' + esc(cost) + '</td></tr>' +
    '</table>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em">Live Log (last 20 lines)</div>' +
    '<div style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:4px;padding:8px;max-height:240px;overflow-y:auto">' + logsHtml + '</div>';
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
            '<span class="ap-pill">Phase: <strong>' + esc(phase) + '</strong></span>' +
            '<span class="ap-pill">Cycles: <strong>' + esc(String(cycleCount)) + '</strong></span>' +
            '<span class="ap-pill">Last poll: <strong>' + pollHint + '</strong></span>' +
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
    var stage = sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : '—';
    var title = sess.card_title || sess.card_id || '—';
    var dispatchAt = sess.agent_dispatch_at || s.agent_dispatch_at;
    var elapsed = dispatchAt ? (fmtAge(dispatchAt) || '—') : '—';
    var phaseLabel = formatPhaseLabel(sess.phase || 'active');
    var cardId = sess.card_id || '—';
    html += '<div class="ap-tile">' +
      '<div class="ap-tile-header">' +
        '<div class="ap-tile-role"><span class="perf-dot perf-running"></span>' + stage + '</div>' +
        '<div class="ap-tile-phase">' + phaseLabel + '</div>' +
      '</div>' +
      '<div class="ap-tile-title">' + esc(title) + '</div>' +
      '<div class="ap-tile-meta">' +
        '<span class="ap-pill ap-tile-elapsed">&#9201; <strong>' + esc(elapsed) + '</strong></span>' +
        '<span class="ap-pill">Card: <strong>' + esc(cardId) + '</strong></span>' +
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

function hidePerformerRoleDetail() {
  _performersDetailOpen = false;
  var listView = document.getElementById('performers-page-list-view');
  var detailView = document.getElementById('performers-page-detail-view');
  if (listView) listView.style.display = '';
  if (detailView) detailView.style.display = 'none';
}

function showPerformerRoleDetail(role) {
  _performersSelectedRole = role;
  _performersDetailOpen = true;
  if (_lastState) renderPerformersPage(_lastState);
}

// 049 + 053: Performers page list + drilldown detail
function renderPerformersPage(s) {
  var tbody = document.getElementById('performers-page-tbody');
  if (!tbody) return;
  var util = s.role_utilization || [];
  var listView = document.getElementById('performers-page-list-view');
  var detailView = document.getElementById('performers-page-detail-view');
  var detailEl = document.getElementById('performers-page-detail');
  if (util.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No performer roles configured</td></tr>';
    if (listView) listView.style.display = '';
    if (detailView) detailView.style.display = 'none';
    return;
  }

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
  if (_performersSelectedRole == null && util.length > 0) {
    _performersSelectedRole = util[0].role;
  }

  tbody.innerHTML = util.map(function(r) {
    var isActive = r.active > 0;
    var isSelected = _performersDetailOpen && _performersSelectedRole === r.role;
    var badge = '<span class="role-status-badge ' + (isActive ? 'role-active' : 'role-idle') + '">' + (isActive ? 'active' : 'idle') + '</span>';
    var cards = (activeByStage[r.role] || []).map(function(sess) {
      return esc((sess.card_title || sess.card_id || '').substring(0, 40));
    }).join('<br>') || '<span class="empty-state">—</span>';
    var maxVal = r.max > 0 ? r.max : 1;
    var activeVal = isActive ? (r.active || 1) : 0;
    var idleVal = Math.max(0, maxVal - activeVal);
    var utilStr = activeVal + ' / ' + idleVal + ' / ' + maxVal;
    var queued = r.queued > 0 ? ' <span style="color:var(--color-accent-yellow)">(+' + r.queued + ' queued)</span>' : '';
    return '<tr class="performers-row' + (isSelected ? ' row-selected' : '') + '" data-role="' + esc(r.role) + '" tabindex="0" role="button" aria-label="Open details for ' + esc(r.role) + '">' +
      '<td style="font-weight:bold">' + esc(r.role) + '</td><td>' + badge + '</td><td style="font-size:12px">' + cards + '</td><td>' + utilStr + queued + '</td></tr>';
  }).join('');

  tbody.querySelectorAll('tr[data-role]').forEach(function(row) {
    row.addEventListener('click', function() {
      var role = row.getAttribute('data-role') || '';
      showPerformerRoleDetail(role);
    });
    row.addEventListener('keydown', function(event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        var role = row.getAttribute('data-role') || '';
        showPerformerRoleDetail(role);
      }
    });
  });

  if (!_performersDetailOpen || !_performersSelectedRole) {
    if (listView) listView.style.display = '';
    if (detailView) detailView.style.display = 'none';
    return;
  }
  if (!listView || !detailView || !detailEl) return;

  var selectedRole = _performersSelectedRole;
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
  var tokenTotal = derivePerformerTokenTotal(sel);
  var metricsLine = (metrics && metrics.pid != null)
    ? (
      'PID ' + String(metrics.pid) +
      ' · CPU ' + esc(metrics.cpu_percent != null ? metrics.cpu_percent.toFixed(1) + '%' : '—') +
      ' · Memory ' + esc(fmtBytes(metrics.memory_bytes)) +
      ' · Tokens ' + esc(tokenTotal != null ? tokenTotal.toLocaleString() : '—')
    )
    : (tokenTotal != null
        ? 'Tokens ' + esc(tokenTotal.toLocaleString()) + ' · no process metrics (containerised performer)'
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
  var skipReasons = s.session_skip_reasons || {};
  var roleSkips = [];
  selectedSessions.forEach(function(sess) {
    var reason = skipReasons[sess.card_id];
    if (reason) roleSkips.push({card: sess.card_title || sess.card_id, reason: reason});
  });
  var skipHtml = roleSkips.length
    ? '<ul class="detail-list">' + roleSkips.map(function(sr) {
        var r = sr.reason || {};
        var label = r.reason === 'pipeline_capacity' ? 'Queued — issue limit reached' : esc(r.reason || 'skipped');
        var blockers = Array.isArray(r.blockers) && r.blockers.length
          ? ' (blocked by #' + r.blockers.map(function(n) { return esc(String(n)); }).join(', #') + ')' : '';
        return '<li><span style="color:var(--color-accent-orange)">' + esc(sr.card) + '</span> — ' + label + blockers + '</li>';
      }).join('') + '</ul>'
    : '';

  detailEl.innerHTML =
    '<div><strong>' + humanPhase(selectedRole) + '</strong>' + statusBadge + '</div>' +
    '<div class="muted" style="margin-top:4px">Active / Idle / Max: ' + String(row.active != null ? row.active : 0) + ' / ' + String(Math.max(0, (row.max != null ? row.max : 0) - (row.active != null ? row.active : 0))) + ' / ' + String(row.max != null ? row.max : 0) + '</div>' +
    '<div class="detail-block"><strong>Card Context</strong>' + roleCardsHtml + telemetryFor + stepTrailHtml(sel, s.stall_timeout_seconds) + (skipHtml ? '<div style="margin-top:8px"><span style="color:var(--color-accent-yellow);font-size:12px">Skipped this cycle:</span>' + skipHtml + '</div>' : '') + '</div>' +
    '<div class="detail-block"><strong>Session</strong><div class="muted" style="margin-top:4px">Session: ' + esc(sel.session_id || s.agent_session_id || '—') + ' &middot; Uptime: ' + esc(fmtAge(sel.agent_dispatch_at || s.agent_dispatch_at) || '—') + '</div><div class="muted" style="margin-top:4px">' + backendLine + '</div><div class="muted" style="margin-top:4px">' + statsLine + '</div></div>' +
    '<div class="detail-block"><strong>Metrics</strong><div class="muted" style="margin-top:4px">' + metricsLine + '</div></div>' +
    '<div class="detail-block"><strong>Live Events</strong>' + eventsHtml + '</div>' +
    '<div class="detail-block"><strong>Process Logs (stderr)</strong>' + logsHtml + '</div>';
  listView.style.display = 'none';
  detailView.style.display = '';
}
