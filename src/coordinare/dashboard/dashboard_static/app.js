function phaseClass(phase) {
  if (phase === 'idle') return 'phase-idle';
  if (phase === 'merging') return 'phase-merging';
  if (phase === 'blocked') return 'phase-blocked';
  if (phase === 'recovery') return 'phase-recovery';
  return 'phase-monitoring';
}

// fmtDuration/fmtTime/fmtAge: /static/helpers.js (#349)
// 343: the step trail. Completed steps from the observed transitions, the
// current one marked and carrying a live elapsed timer seeded from snapshot
// state -- so it survives a browser reload and a daemon restart, neither of
// which a client-side counter would.
function stepTrailHtml(sess, slowAfterSeconds) {
  var cur = sess.workflow_step;
  if (!cur) return '';
  var trail = Array.isArray(sess.workflow_step_trail) ? sess.workflow_step_trail : [];
  var shortName = function(n) { var s = String(n || ''); var i = s.indexOf('.'); return i < 0 ? s : s.slice(i + 1); };
  // Only steps before the current one are done; the current entry is the last.
  var done = trail.slice(0, Math.max(0, trail.length - 1));
  var parts = done.slice(-6).map(function(e) {
    return '<span class="muted">' + esc(shortName(e.step)) + ' &check;</span>';
  });
  var age = fmtAge(sess.workflow_step_entered_at);
  var secs = sess.workflow_step_entered_at
    ? Math.floor((Date.now() - new Date(sess.workflow_step_entered_at).getTime()) / 1000) : 0;
  // 0 means the operator has not said what "too long" is, so say nothing.
  var slow = slowAfterSeconds > 0 && secs > slowAfterSeconds;
  var style = slow ? 'color:var(--color-degraded)' : 'color:var(--color-accent-blue)';
  parts.push('<span style="' + style + ';font-weight:bold" data-step-since="'
    + esc(sess.workflow_step_entered_at || '') + '">&bull; ' + esc(shortName(cur))
    + (age ? ' ' + esc(age) : '') + '</span>');
  var more = done.length > 6 ? '<span class="muted">+' + String(done.length - 6) + ' earlier &middot; </span>' : '';
  return '<div class="step-trail" style="margin-top:4px;font-size:12px">' + more + parts.join('<span class="muted"> &middot; </span>') + '</div>';
}

// humanPhase + formatPhaseLabel: /static/helpers.js (#349)
function computeOverallHealth(subsystems) {
  var required = (subsystems || []).filter(function(s) { return s.required; });
  if (!required.length || required.every(function(s) { return s.status === 'healthy'; })) return 'healthy';
  if (required.some(function(s) { return s.status === 'unavailable'; })) return 'unavailable';
  return 'degraded';
}

function isIdle(s) {
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  return sessions.length === 0 && s.phase === 'idle';
}

// 128: the Dashboard panel shows collapsed rows and a fixed number of them
// (opts.collapse, opts.limit); the History page keeps the full list.
function renderCycleHistoryInto(container, cycleHistory, opts) {
  if (!container) return;
  opts = opts || {};
  var history = Array.isArray(cycleHistory) ? cycleHistory : [];
  if (history.length === 0) {
    container.innerHTML = '<span class="empty-state">No checks completed yet</span>';
    return;
  }
  var rows = opts.collapse ? collapseCycleRows(history) : history.map(function(e) {
    return { phase: e.phase, outcome: e.outcome || 'success', count: 1, timestamp: e.timestamp, duration_seconds: e.duration_seconds };
  });
  var hidden = 0;
  if (opts.limit && rows.length > opts.limit) {
    hidden = rows.length - opts.limit;
    rows = rows.slice(0, opts.limit);
  }
  container.innerHTML = '<div class="cycle-history' + (opts.limit ? ' cycle-history-compact' : '') + '"><table><thead><tr>' +
    '<th>Time</th><th>Status</th><th>Duration</th><th>Outcome</th>' +
    '</tr></thead><tbody>' +
    rows.map(function(r) {
      var many = r.count > 1;
      return '<tr>' +
        '<td>' + fmtTime(r.timestamp) + '</td>' +
        '<td>' + humanPhase(r.phase) + (many ? ' <span class="cycle-count" title="' + r.count + ' consecutive checks with the same result">' + r.count + ' checks</span>' : '') + '</td>' +
        '<td>' + (many ? 'avg ' : '') + fmtDuration(r.duration_seconds) + '</td>' +
        '<td><span class="badge badge-' + esc(r.outcome) + '">' + (many ? 'all ' : '') + esc(r.outcome) + '</span></td>' +
        '</tr>';
    }).join('') +
    '</tbody></table></div>' +
    (hidden > 0 ? '<div class="cycle-history-more">' + hidden + ' older ' + (hidden === 1 ? 'row' : 'rows') + ' on the History page</div>' : '');
}

function findActivePerformerSession(s) {
  // Multi-card mode: flat top-level fields can reflect any session's last sync.
  // For aggregate UI indicators, derive from active_sessions instead.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  for (var i = 0; i < sessions.length; i++) {
    var p = sessions[i].phase;
    if (p === 'monitoring_performer' || p === 'monitoring_agent' || p === 'relay_feedback') {
      return sessions[i];
    }
  }
  return null;
}

function renderState(s) {
  // Phase — prefer aggregate session state over flat top-level fields, which
  // get clobbered by whichever session last synced to CoordinareState.
  var activeSess = findActivePerformerSession(s);
  var effectivePhase = activeSess ? activeSess.phase : s.phase;
  var effectiveDispatchAt = activeSess ? activeSess.agent_dispatch_at : s.agent_dispatch_at;
  var phaseEl = document.getElementById('phase');
  // 138: a plain sentence; the conductor's internal state stays in the tooltip.
  phaseEl.textContent = conductorHeadline(s, activeSess);
  phaseEl.title = 'Conductor state: ' + String(effectivePhase || '');
  phaseEl.className = 'phase ' + phaseClass(effectivePhase);
  var phaseDescriptions = {
    'idle':             'Waiting for a card to enter the TODO column on the GitHub Project board.',
    'running':          'Running a check.',
    'blocked':          'Work is paused — review the Open Questions below and take action.',
    'dispatching':      'Assessing and dispatching card to the performer agent.',
    'monitoring_agent': 'Performer agent is actively working on the card.',
    'monitoring_performer': 'Performer agent is actively working on the card.',
    'monitoring_pr':    'Waiting for PR review approval.',
    'merging':          'Merging the approved pull request.',
    'relay_feedback':   'Relaying PR review feedback to the performer agent.',
    'recovery':         'A check failed; the daemon is recovering before retrying.',
  };
  document.getElementById('phase-desc').textContent = phaseDescriptions[effectivePhase] || '';

  // Session age (shown when a performer is actively running on any session)
  var ageEl = document.getElementById('session-age');
  if (activeSess && effectiveDispatchAt) {
    ageEl.style.display = '';
    // 125: same start point and same refresh timer as the performer tile.
    ageEl.setAttribute('data-elapsed-since', effectiveDispatchAt);
    ageEl.setAttribute('data-elapsed-prefix', 'Stage running for: ');
    ageEl.title = 'Time since this stage run was dispatched';
    ageEl.textContent = 'Stage running for: ' + (fmtAge(effectiveDispatchAt) || '—');
  } else {
    ageEl.style.display = 'none';
    ageEl.removeAttribute('data-elapsed-since');
  }

  // Flowchart (async)
  updateFlowChart(s.phase);

  // Active Work + Awaiting Review panels
  renderActiveWorkPanels(s);

  // Idle state panel (059 Phase G)
  var idlePanel = document.getElementById('idle-panel');
  var workCard = document.getElementById('active-work-card');
  if (idlePanel) {
    if (isIdle(s)) {
      idlePanel.style.display = '';
      if (workCard) workCard.style.display = '';
      var idleSummary = (s.board_summary && typeof s.board_summary === 'object') ? s.board_summary : {};
      var bCount = function(k) { var v = idleSummary[k]; return Number.isFinite(v) ? v : 0; };
      var idleTotal = bCount('TODO') + bCount('IN_PROGRESS') + bCount('IN_REVIEW') + bCount('DONE');
      var idleInProg = bCount('IN_PROGRESS');
      var idlePoll = s.last_poll_at ? esc(fmtTime(s.last_poll_at)) : '\u2014';
      var idleCycles = s.cycles_completed != null ? esc(String(s.cycles_completed)) : '\u2014';
      var idleFilterText = s.ownership_hint ? esc(s.ownership_hint) : '';
      var idleFilter = idleFilterText ? ' <span style="color:var(--color-text-muted);font-size:12px">(filter: ' + idleFilterText + ')</span>' : '';
      document.getElementById('idle-panel-content').innerHTML =
        '<table style="border-collapse:collapse;font-size:13px">' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Board total</td>' +
              '<td style="padding:4px 0"><strong>' + idleTotal + '</strong>' + idleFilter + '</td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">In progress</td>' +
              '<td style="padding:4px 0"><strong>' + idleInProg + '</strong></td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Last checked</td>' +
              '<td style="padding:4px 0"><strong>' + idlePoll + '</strong></td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Checks completed</td>' +
              '<td style="padding:4px 0"><strong>' + idleCycles + '</strong></td></tr>' +
        '</table>';
    } else {
      idlePanel.style.display = 'none';
      if (workCard) workCard.style.display = '';
    }
  }

  // T032: Live-update card detail view if one is open
  if (_selectedCardId) {
    var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
    var activeSess = sessions.find(function(ss) { return ss.card_id === _selectedCardId; });
    if (activeSess) {
      var detailContent = document.getElementById('card-detail-content');
      if (detailContent) detailContent.innerHTML = renderCardDetailContent(activeSess, s);
    } else {
      // Session ended while detail was open — close back to the list.
      closeCardDetail();
    }
  }

  // 046: Dependency blockers
  var depCard = document.getElementById('dependency-blockers-card');
  if (!depCard) {
    depCard = document.createElement('div');
    depCard.id = 'dependency-blockers-card';
    depCard.className = 'card';
    depCard.style.display = 'none';
    depCard.innerHTML = '<h3>Blocked by Dependencies</h3><ul id="dependency-blockers-list"></ul>';
    var questionsCard = document.getElementById('questions-card');
    if (questionsCard && questionsCard.parentNode) {
      questionsCard.parentNode.insertBefore(depCard, questionsCard);
    }
  }
  var depList = document.getElementById('dependency-blockers-list');
  if (s.blocked_by_dependencies && s.blocked_by_dependencies.length > 0) {
    depCard.style.display = '';
    depList.innerHTML = s.blocked_by_dependencies.map(function(d) {
      var label = '#' + d.issue_number;
      if (d.title) label += ' — ' + esc(d.title);
      var col = d.column || 'off-board';
      var link = d.issue_url && /^https?:\/\//i.test(d.issue_url)
        ? '<a href="' + esc(d.issue_url) + '" target="_blank" rel="noopener">' + esc(label) + '</a>'
        : esc(label);
      return '<li>' + link + ' <span style="opacity:0.7">(' + esc(col) + ')</span></li>';
    }).join('');
  } else {
    depCard.style.display = 'none';
  }

  // 048: Performer utilization
  var utilCard = document.getElementById('role-utilization-card');
  if (!utilCard) {
    utilCard = document.createElement('div');
    utilCard.id = 'role-utilization-card';
    utilCard.className = 'card';
    utilCard.style.display = 'none';
    utilCard.innerHTML = '<h3>Performer Utilization</h3><table id="role-utilization-table" style="width:100%;border-collapse:collapse"><thead><tr><th style="text-align:left">Stage · Agent</th><th>Busy</th></tr></thead><tbody></tbody></table>';
    var utilizationAnchor = document.getElementById('recent-cycles-card');
    if (utilizationAnchor && utilizationAnchor.parentNode) { utilizationAnchor.parentNode.insertBefore(utilCard, utilizationAnchor); }
  }
  var utilTbody = utilCard.querySelector('tbody');
  if (s.role_utilization && s.role_utilization.length > 0) {
    utilCard.style.display = '';
    utilTbody.innerHTML = s.role_utilization.map(utilizationRowHtml).join('');
  } else {
    utilCard.style.display = 'none';
  }

  // Open questions (355: card-attributed; the server scopes the list to
  // sessions still in the blocked phase, so a question never outlives its
  // card's blocked state). Each entry is {card_id, card_number, card_title,
  // stage, text, asked_at, issue_url}; a bare legacy string renders as the
  // question text alone with the panel-level issue link.
  var qCard = document.getElementById('questions-card');
  var qList = document.getElementById('questions-list');
  if (s.open_questions && s.open_questions.length > 0) {
    qCard.style.display = '';
    qList.innerHTML = s.open_questions.map(function(q) {
      var obj = (typeof q === 'string') ? { text: q } : (q || {});
      var text = (typeof obj.text === 'string') ? obj.text : '';
      var head = '';
      if (obj.card_number != null) {
        var num = '#' + String(obj.card_number);
        head = obj.issue_url
          ? '<a href="' + esc(obj.issue_url) + '" target="_blank" rel="noopener">' + esc(num) + '</a>'
          : esc(num);
        if (obj.card_title) head += ' ' + esc(obj.card_title);
      } else if (obj.card_title) {
        head = esc(obj.card_title);
      }
      if (obj.stage) head += ' <span class="muted">(' + esc(obj.stage) + ')</span>';
      var ageTxt = obj.asked_at ? fmtAge(obj.asked_at) : null;
      var age = ageTxt ? ' <span class="muted">asked ' + esc(ageTxt) + ' ago</span>' : '';
      var body = head ? head + ': ' + esc(text) : esc(text);
      if (!obj.card_number) {
        var issueHref = s.issue_url && /^https?:\/\//i.test(s.issue_url) ? s.issue_url : null;
        if (issueHref) {
          body += ' <a href="' + esc(issueHref) + '" target="_blank" rel="noopener" style="font-size:0.85em;white-space:nowrap">View issue &#8599;</a>';
        }
      }
      return '<li>' + body + age + '</li>';
    }).join('');
  } else {
    qCard.style.display = 'none';
  }

  // 047: Rebase status
  var rebaseCard = document.getElementById('rebase-status-card');
  if (!rebaseCard) {
    rebaseCard = document.createElement('div');
    rebaseCard.id = 'rebase-status-card';
    rebaseCard.className = 'card';
    rebaseCard.style.display = 'none';
    rebaseCard.innerHTML = '<h3>Branch Divergence</h3><ul id="rebase-status-list"></ul>';
    var anchor = document.getElementById('questions-card');
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(rebaseCard, anchor);
    }
  }
  var rebaseList = document.getElementById('rebase-status-list');
  if (s.last_rebase_round && s.last_rebase_round.jobs && s.last_rebase_round.jobs.length > 0) {
    rebaseCard.style.display = '';
    var outcomeEmoji = {delegated: '\ud83d\udc77', deferred: '\u23f3', clean: '\u2705', performer_resolved: '\ud83d\udee0', blocked: '\ud83d\udeab', skipped: '\u23e9', failed: '\u274c'};
    rebaseList.innerHTML = s.last_rebase_round.jobs.map(function(j) {
      var emoji = outcomeEmoji[j.outcome] || '\u2753';
      var sha = j.post_rebase_sha ? j.post_rebase_sha.substring(0, 8) : '';
      var label = emoji + ' ' + esc(j.branch.split('/').pop() || j.branch) + ' — ' + esc(j.outcome);
      if (sha) label += ' (' + esc(sha) + ')';
      if (j.conflicted_files && j.conflicted_files.length > 0) {
        label += ' [' + j.conflicted_files.map(esc).join(', ') + ']';
      }
      return '<li>' + label + '</li>';
    }).join('');
  } else {
    rebaseCard.style.display = 'none';
  }

  // Clarification history (355: each round labelled with the card and stage
  // it belongs to; rounds stay collapsed as before)
  var clCard = document.getElementById('clarifications-card');
  var clList = document.getElementById('clarifications-list');
  if (s.card_clarifications && s.card_clarifications.length > 0) {
    clCard.style.display = '';
    clList.innerHTML = s.card_clarifications.map(function(round, i) {
      round = round || {};
      var qs = (round.questions || []).map(function(q) {
        return '<li>' + esc(q) + '</li>';
      }).join('');
      var ans = round.answer ? '<div class="qa-round-a">&#x1F4AC; ' + esc(round.answer) + '</div>' : '';
      var comment = '';
      if (round.body) {
        var source = esc(round.source || 'comment');
        if (round.comment_id != null) source += ' #' + esc(String(round.comment_id));
        if (round.author) source += ' · ' + esc(round.author);
        comment = '<div class="muted" style="font-size:11px">' + source + '</div>' +
          '<div class="qa-round-a" style="white-space:pre-wrap">' + esc(round.body) + '</div>';
      }
      var attr = '';
      if (round.card_number != null) {
        var num = '#' + String(round.card_number);
        attr = round.issue_url
          ? '<a href="' + esc(round.issue_url) + '" target="_blank" rel="noopener">' + esc(num) + '</a>'
          : esc(num);
        if (round.card_title) attr += ' ' + esc(round.card_title);
      } else if (round.card_title) {
        attr = esc(round.card_title);
      }
      if (round.stage) attr += ' <span class="muted">(' + esc(round.stage) + ')</span>';
      var header = 'Round ' + (i + 1) + (attr ? ' — ' + attr : '');
      return '<div class="qa-round">' +
        '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:4px">' + header + '</div>' +
        (qs ? '<div class="qa-round-q"><ul>' + qs + '</ul></div>' : '') +
        ans + comment +
        '</div>';
    }).join('');
  } else {
    clCard.style.display = 'none';
  }

  // Metrics
  document.getElementById('cycles-completed').textContent = s.cycles_completed;
  document.getElementById('last-duration').textContent = fmtDuration(s.last_cycle_duration_seconds);
  document.getElementById('error-count').textContent = s.consecutive_error_count;
  // 119: uptime at a glance, the exact start time on hover.
  var daemonStartEl = document.getElementById('daemon-start-time');
  var daemonUptime = fmtAge(s.daemon_start_time);
  daemonStartEl.textContent = daemonUptime ? 'Up ' + daemonUptime : '—';
  daemonStartEl.title = s.daemon_start_time ? 'Started ' + fmtTime(s.daemon_start_time) : '';
  if (s.project_board_url && s.project_name) {
    var linkEl = document.getElementById('project-link');
    linkEl.textContent = '— ';
    var a = document.createElement('a');
    a.href = s.project_board_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.style.cssText = 'text-decoration:none';
    a.textContent = s.project_name + ' Board';
    linkEl.appendChild(a);
  }
  // Shared-pool usage lives on the monitored session.
  var tokenSource = activeSess || s;
  var cardTokensTotal = (tokenSource.card_tokens_total || s.card_tokens_total || 0);
  if (!(cardTokensTotal > 0)) {
    var fallbackTokens = derivePerformerTokenTotal(tokenSource);
    if (fallbackTokens == null && tokenSource !== s) fallbackTokens = derivePerformerTokenTotal(s);
    if (fallbackTokens != null) cardTokensTotal = fallbackTokens;
  }
  // 122: while a stage runs with nothing reported yet, show pending, not 0.
  var tokenView = tokenReadout(cardTokensTotal, !!activeSess);
  var tokensEl = document.getElementById('card-tokens-total');
  tokensEl.textContent = tokenView.text;
  tokensEl.title = tokenView.title;
  // 121: cost reads the same source as the tokens beside it, and falls back to
  // the displayed token count when the daemon has not priced the session yet.
  var costSource = tokenSource;
  if (!(cardTokensTotal > 0) || estimateCost(costSource, s).value <= 0) {
    costSource = { card_cost_estimate: s.card_cost_estimate, card_tokens_total: cardTokensTotal };
  }
  var costEstimate = estimateCost(costSource, s);
  var costEl = document.getElementById('card-cost-estimate');
  costEl.textContent = tokenView.pending ? '—' : fmtCost(costEstimate.value);
  // 144: say which card the figure is for.
  var tokenCardRef = cardRefText(activeSess || s);
  document.getElementById('card-tokens-label').textContent = 'Tokens' + (tokenCardRef && /^#/.test(tokenCardRef) ? ' · ' + tokenCardRef : '');
  // 144: "Check now" sits beside when the board was last checked.
  var lastCheckedEl = document.getElementById('last-checked');
  if (s.last_poll_at) {
    lastCheckedEl.setAttribute('data-elapsed-since', s.last_poll_at);
    lastCheckedEl.setAttribute('data-elapsed-prefix', 'Last checked ');
    lastCheckedEl.setAttribute('data-elapsed-suffix', ' ago');
    lastCheckedEl.textContent = 'Last checked ' + (fmtAge(s.last_poll_at) || '—') + ' ago';
  } else {
    lastCheckedEl.removeAttribute('data-elapsed-since');
    lastCheckedEl.textContent = '';
  }
  costEl.title = tokenView.pending ? tokenView.title : costEstimate.basis;

  // Agent session
  var agentEl = document.getElementById('agent-session');
  if (s.agent_session_id) {
    agentEl.style.display = '';
    document.getElementById('agent-session-id').textContent = s.agent_session_id;
  } else {
    agentEl.style.display = 'none';
  }

  // Subsystems
  var subsEl = document.getElementById('subsystems-section');
  if (!s.subsystems || s.subsystems.length === 0) {
    subsEl.innerHTML = '<span class="empty-state">No subsystems registered</span>';
  } else {
    var overall = s.overall_health || computeOverallHealth(s.subsystems);
    var isHealthy = overall === 'healthy';
    var summaryIcon = isHealthy ? '&#9679;' : (overall === 'unavailable' ? '&#10005;' : '&#9888;');
    var summaryClass = isHealthy ? 'color:var(--color-healthy)' : (overall === 'unavailable' ? 'color:var(--color-error)' : 'color:var(--color-degraded)');
    var degradedCount = s.subsystems.filter(function(sub) { return sub.required && sub.status !== 'healthy'; }).length;
    var summaryText = isHealthy
      ? 'All systems healthy'
      : (degradedCount + (degradedCount === 1 ? ' system ' : ' systems ') + (overall === 'unavailable' ? 'unavailable' : 'degraded'));
    var rows = s.subsystems.map(function(sub) {
      var rowStyle = sub.status !== 'healthy' ? ' style="background:var(--color-bg-' + (sub.status === 'unavailable' ? 'error' : 'degraded') + ')"' : '';
      return '<tr' + rowStyle + '>' +
        '<td>' + esc(sub.name) + '</td>' +
        '<td><span class="badge badge-' + esc(sub.status) + '">' + esc(sub.status) + '</span></td>' +
        '<td><span class="badge ' + (sub.required ? 'badge-required' : 'badge-optional') + '">' +
          (sub.required ? 'required' : 'optional') + '</span></td>' +
        '<td>' + esc(sub.details || '') + '</td>' +
        '</tr>';
    }).join('');
    subsEl.innerHTML =
      '<details' + (isHealthy ? '' : ' open') + '>' +
        '<summary style="cursor:pointer;list-style:none;padding:2px 0">' +
          '<span style="' + summaryClass + '">' + summaryIcon + ' ' + esc(summaryText) + '</span>' +
        '</summary>' +
        '<div style="margin-top:8px">' +
          '<table><thead><tr>' +
            '<th>Subsystem</th><th>Status</th><th>Required</th><th>Details</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table>' +
        '</div>' +
      '</details>';
  }

  // Force-poll button state (016-force-poll)
  var fpBtn = document.getElementById('force-poll-btn');
  var fpMsg = document.getElementById('force-poll-msg');
  if (fpBtn) {
    var shouldDisable = s.cycle_active || !s.daemon_running;
    fpBtn.disabled = shouldDisable;
    if (!shouldDisable) fpMsg.textContent = '';
  }

  renderCycleHistoryInto(document.getElementById('history-section'), s.cycle_history, { collapse: true, limit: 5 });
}

// esc + fmtBytes: /static/helpers.js (#349)
var STALE_THRESHOLD_MS = 30 * 60 * 1000;  // 30 minutes

var _selectedCardId = null;   // card_id shown in card-detail-view
var _swimlaneTab = null;      // currently selected symphony tab (null = no symphonies)
function selectSwimlaneTab(name) {
  _swimlaneTab = name;
  _swimlaneTabExplicit = true;
  if (_lastState) renderActiveWorkPanels(_lastState);
}

// parseTokenCount + derivePerformerTokenTotal: /static/performers.js (#349)
// Refresh session age counter every 10s while monitoring_agent
var _lastState = null;

// 049: Client-side router
function showPage(pageId) {
  var pages = ['dashboard-page','performers-page','personas-page','history-page','glossary-page','symphonies-page','admin-config-page','config-page','assistant-page'];
  pages.forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = id === pageId ? '' : 'none';
  });
}

function setActiveNav(path) {
  var links = document.querySelectorAll('.nav-link');
  links.forEach(function(link) {
    var href = link.getAttribute('href');
    var active = (path === '/' && href === '/') || (path !== '/' && href !== '/' && path.startsWith(href));
    link.classList.toggle('nav-active', active);
    if (active) { link.setAttribute('aria-current', 'page'); }
    else { link.removeAttribute('aria-current'); }
  });
}

function updateNavActive(path) {
  setActiveNav(path);
  var titles = {'/':'Dashboard — Coordinare','/performers':'Performers — Coordinare','/personas':'Personas — Coordinare','/history':'History — Coordinare','/glossary':'Glossary — Coordinare','/symphonies':'Symphonies — Coordinare','/admin/config':'Global Config — Coordinare','/config':'Configuration — Coordinare'};
  var activeLink = document.querySelector('.nav-link.nav-active');
  document.getElementById('workspace-title').textContent = activeLink ? activeLink.textContent.trim() : 'Dashboard';
  document.title = titles[path] || (path.startsWith('/symphonies/') ? 'Symphony — Coordinare' : path.indexOf('/performers/') === 0 ? 'Run detail | Coordinare' : 'Coordinare');
}

function renderSymphoniesPage(s) {
  var el = document.getElementById('symphonies-page-section');
  if (!el) return;
  var syms = Array.isArray(s.symphonies) ? s.symphonies : [];
  var path = location.pathname;
  var detail = path.startsWith('/symphonies/') ? decodeURIComponent(path.slice('/symphonies/'.length)) : null;
  if (detail) {
    // On SSE updates, avoid re-fetching if the page is already rendered:
    // update only the volatile status table cells from the SSE state payload.
    var sym = syms.find(function(x) { return x.name === detail; });
    var tbl = el.querySelector('table[data-detail]');
    if (tbl && sym) {
      var rows = tbl.querySelectorAll('tbody tr');
      function _setTd(row, val) { if (row) { var td = row.querySelector('td'); if (td) td.textContent = val; } }
      _setTd(rows[1], sym.cycle_count != null ? sym.cycle_count : '—');
      _setTd(rows[2], sym.error_count != null ? sym.error_count : '—');
      _setTd(rows[3], sym.state && sym.state.active_card ? (sym.state.active_card.title || sym.state.active_card.id || '') : '—');
      _setTd(rows[4], sym.last_poll_at ? fmtTime(sym.last_poll_at) : '—');
      return;
    }
    loadSymphonyDetail(detail, el);
    return;
  }
  // List view
  var addFormHtml = '<div id="sym-add-form" style="display:none;margin-top:16px;padding:12px;background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:10px;font-size:13px">Add Symphony</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">'
    + '<div><label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">Name (alphanumeric + dash)</label>'
    + '<input id="sym-add-name" type="text" placeholder="e.g. frontend" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px"></div>'
    + '<div><label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">GitHub Project Number</label>'
    + '<input id="sym-add-proj" type="number" min="1" placeholder="e.g. 42" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px"></div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center">'
    + '<button class="action-btn" onclick="submitAddSymphony()">Add</button>'
    + '<button class="action-btn" onclick="document.getElementById(\'sym-add-form\').style.display=\'none\'">Cancel</button>'
    + '<span id="sym-add-msg" class="action-msg"></span>'
    + '</div></div>';
  if (!syms.length) {
    el.innerHTML = '<span class="empty-state">No symphonies configured.</span>'
      + '<div style="margin-top:12px"><button class="action-btn" onclick="document.getElementById(\'sym-add-form\').style.display=\'block\'">+ Add Symphony</button></div>'
      + addFormHtml;
    return;
  }
  var rows = syms.map(function(sym) {
    var bootBtn = sym.env_bootstrap_performer_id
      ? '<button class="action-btn sym-row-bootstrap" data-sym="' + esc(sym.name) + '" style="padding:2px 8px;font-size:11px">Bootstrap</button>'
      : '<span style="color:var(--color-text-muted)">—</span>';
    // 077: at-a-glance bootstrap status badge (colored dot + label) so a failing
    // env bootstrap is visible on the overview without opening the symphony.
    var bootStatus = '';
    if (sym.env_bootstrap_performer_id) {
      var bsColor, bsText;
      if (sym.bootstrap_in_flight) { bsColor = 'var(--color-accent-blue)'; bsText = 'bootstrapping…'; }
      else if (sym.last_bootstrap_succeeded === false) { bsColor = 'var(--color-accent-red)'; bsText = 'failed'; }
      else if (sym.cache_dir_ready && sym.last_bootstrap_succeeded === true) { bsColor = 'var(--color-accent-green)'; bsText = 'ready'; }
      else { bsColor = 'var(--color-text-muted)'; bsText = 'not bootstrapped'; }
      var bsTitle = (sym.last_bootstrap_succeeded === false && sym.last_bootstrap_error)
        ? ' title="' + esc(sym.last_bootstrap_error) + '"' : '';
      bootStatus = '<span' + bsTitle + ' style="display:inline-flex;align-items:center;gap:5px;margin-right:8px;font-size:11px;color:' + bsColor + '">'
        + '<span style="width:8px;height:8px;border-radius:50%;background:' + bsColor + ';display:inline-block;flex:none"></span>'
        + bsText + '</span>';
    }
    return '<tr>'
      + '<td><a href="/symphonies/' + encodeURIComponent(sym.name) + '" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue)">' + esc(sym.name) + '</a></td>'
      + '<td style="color:var(--color-text-muted)">' + (sym.github_project_number != null ? '#' + sym.github_project_number : '—') + '</td>'
      + '<td>' + (sym.priority != null ? sym.priority : '—') + '</td>'
      + '<td>' + (sym.cycle_count != null ? sym.cycle_count : '—') + '</td>'
      + '<td>' + (sym.error_count != null ? sym.error_count : '—') + '</td>'
      + '<td>' + (sym.last_poll_at ? esc(fmtTime(sym.last_poll_at)) : '—') + '</td>'
      + '<td>' + bootStatus + bootBtn + '</td>'
      + '</tr>';
  }).join('');
  el.innerHTML = '<div style="overflow-x:auto"><table style="width:100%;font-size:12px"><thead><tr>'
    + '<th style="text-align:left">Name</th><th style="text-align:left">Project</th><th style="text-align:left">Priority</th><th style="text-align:left">Checks</th><th style="text-align:left">Errors</th><th style="text-align:left">Last checked</th><th style="text-align:left">Env</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    + '<div id="sym-list-msg" class="action-msg" style="margin-top:8px"></div>'
    + '<div style="margin-top:14px"><button class="action-btn" onclick="document.getElementById(\'sym-add-form\').style.display=\'block\'">+ Add Symphony</button></div>'
    + addFormHtml;
  el.querySelectorAll('.sym-row-bootstrap').forEach(function(b) {
    b.addEventListener('click', async function() {
      var n = b.getAttribute('data-sym');
      var msg = document.getElementById('sym-list-msg');
      b.disabled = true;
      msg.textContent = 'Dispatching ' + n + '...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(n) + '/env-bootstrap', {method: 'POST'});
        var d = await r.json();
        if (r.ok) { msg.textContent = n + ': accepted'; msg.style.color = 'var(--color-accent-green)'; }
        else { msg.textContent = n + ': ' + (d.error || ('error ' + r.status)); msg.style.color = 'var(--color-accent-red)'; }
      } catch(e) { msg.textContent = n + ': network error'; msg.style.color = 'var(--color-accent-red)'; }
      b.disabled = false;
    });
  });
}

async function loadSymphonyDetail(name, el) {
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var backLink = '<a href="/symphonies" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue);font-size:13px">&#8592; All symphonies</a>';
  var res, data;
  try {
    res = await fetch('/api/symphonies/' + encodeURIComponent(name));
    _symCfgHash = res.headers.get('ETag');
    data = await res.json();
  } catch(e) {
    el.innerHTML = backLink + '<div class="empty-state" style="margin-top:12px">Failed to load symphony</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = backLink + '<div class="empty-state" style="margin-top:12px">' + esc(data.error || 'Symphony not found') + '</div>';
    return;
  }
  var st = data.state || {};
  var ov = data.overrides || {};
  var personas = data.personas || {};
  var overrideFields = [
    {key:'max_concurrent_cards', label:'Max concurrent cards', type:'number', placeholder:'1'},
    {key:'poll_interval_seconds', label:'Poll interval (seconds)', type:'number', placeholder:'30'},
    {key:'max_feedback_cycles', label:'Max feedback cycles', type:'number', placeholder:'5'},
    {key:'max_closed_pr_attempts_per_issue', label:'Max closed PR attempts', type:'number', placeholder:'3'},
    {key:'assignee_filter', label:'Assignee filter (GitHub login)', type:'text', placeholder:'(no filter)'},
  ];
  var personaRoles = ['assessor','architect','implementer','reviewer','security','qa','tech_writer','closer'];
  var statusRows = [
    ['GitHub project', data.github_project_number != null ? '#' + data.github_project_number : '—'],
    ['Cycles', st.cycle_count != null ? st.cycle_count : '—'],
    ['Errors', st.error_count != null ? st.error_count : '—'],
    ['Active card', st.active_card ? esc(st.active_card.title || st.active_card.id || '') : '—'],
    ['Last checked', st.last_poll_at ? esc(fmtTime(st.last_poll_at)) : '—'],
    ['Last error', st.last_error ? esc(st.last_error) : '—'],
  ].map(function(r) {
    return '<tr><th style="text-align:left;padding:3px 10px 3px 0;color:var(--color-text-muted);font-weight:normal;white-space:nowrap">' + r[0] + '</th><td style="font-size:12px">' + r[1] + '</td></tr>';
  }).join('');
  var overrideInputs = overrideFields.map(function(f) {
    var val = ov[f.key] != null ? ov[f.key] : '';
    return '<div style="margin-bottom:8px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(f.label) + '</label>'
      + '<input data-override-key="' + esc(f.key) + '" type="' + f.type + '" value="' + esc(String(val)) + '" placeholder="' + esc(f.placeholder) + '" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px">'
      + '</div>';
  }).join('');
  var enabledChecked = data.enabled !== false ? 'checked' : '';
  var specFiles = data.env_spec_files || ['README.md'];
  var specFileTags = specFiles.map(function(f) {
    return '<span data-spec-file="' + esc(f) + '" style="display:inline-flex;align-items:center;gap:4px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:12px;padding:2px 8px;font-size:11px;margin:2px">'
      + esc(f)
      + '<button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;cursor:pointer;color:var(--color-text-muted);font-size:13px;padding:0 0 0 2px;line-height:1">&times;</button>'
      + '</span>';
  }).join('');
  var personaInputs = personaRoles.map(function(role) {
    var instr = (personas[role] && personas[role].instructions) ? personas[role].instructions : '';
    return '<div style="margin-bottom:10px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(role) + '</label>'
      + '<textarea data-persona-role="' + esc(role) + '" rows="3" placeholder="(inherits global default)" style="width:100%;box-sizing:border-box;font-family:monospace;font-size:11px;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:6px 8px;resize:vertical">' + esc(instr) + '</textarea>'
      + '</div>';
  }).join('');
  var ec = data.env_cache;
  var ecBootstrapId = data.env_bootstrap_performer_id;
  var ecSection = '';
  if (ecBootstrapId) {
    var ecRows;
    if (ec) {
      var inFlight = ec.bootstrap_in_flight;
      var ready = ec.cache_dir_ready;
      var lastOk = ec.last_bootstrap_succeeded;
      var statusColor;
      var statusText;
      if (inFlight) { statusColor = 'var(--color-accent-blue)'; statusText = 'in flight'; }
      else if (lastOk === true && ready) { statusColor = 'var(--color-accent-green)'; statusText = 'ready'; }
      else if (lastOk === false) { statusColor = 'var(--color-accent-red)'; statusText = 'last bootstrap failed'; }
      else { statusColor = 'var(--color-text-muted)'; statusText = 'not yet bootstrapped'; }
      var infRow;
      if (ec.last_inference_skipped_reason) {
        infRow = '<span style="color:var(--color-text-muted)">skipped: '
          + esc(ec.last_inference_skipped_reason) + '</span>';
      } else if (ec.last_inference_succeeded === true) {
        var svcs = (ec.last_inference_services || []).join(', ') || '(none)';
        infRow = '<span style="color:var(--color-accent-green)">ok</span> '
          + esc(svcs)
          + ' <span style="color:var(--color-text-muted)">('
          + esc(String(ec.last_inference_attempts || '?')) + ' attempt'
          + ((ec.last_inference_attempts || 0) === 1 ? '' : 's') + ', '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else if (ec.last_inference_succeeded === false) {
        infRow = '<span style="color:var(--color-accent-red)">failed</span> '
          + '<span style="color:var(--color-text-muted)">('
          + esc(String(ec.last_inference_attempts || '?')) + ' attempts, '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else {
        infRow = '—';
      }
      ecRows = [
        ['Status', '<span style="color:' + statusColor + '">' + statusText + '</span>'],
        ['Performer', esc(ecBootstrapId)],
        ['Cache dir', esc(ec.cache_dir || '—')],
        ['Cache ready', ready ? 'yes' : 'no'],
        ['Bootstrap in flight', inFlight ? 'yes' : 'no'],
        ['Last bootstrap', ec.last_bootstrap_at ? esc(fmtTime(ec.last_bootstrap_at)) : '—'],
        ['Recorded SHA', ec.readme_sha ? esc(ec.readme_sha) : '—'],
        ['Service inference', infRow],
        ['Inference at', ec.last_inference_at ? esc(fmtTime(ec.last_inference_at)) : '—'],
      ].map(function(r) {
        return '<tr><th style="text-align:left;padding:3px 10px 3px 0;color:var(--color-text-muted);font-weight:normal;white-space:nowrap">' + r[0] + '</th><td style="font-size:12px">' + r[1] + '</td></tr>';
      }).join('');
    } else {
      ecRows = '<tr><td style="font-size:12px;color:var(--color-text-muted)">Env cache state not initialised yet.</td></tr>';
    }
    var disabled = (ec && ec.bootstrap_in_flight) ? 'disabled' : '';
    // 077: prominent error banner when the last bootstrap failed, with the reason
    // (verify failure / dispatch error / reap reason) so an operator sees WHY.
    var ecErrBanner = (ec && ec.last_bootstrap_succeeded === false && ec.last_bootstrap_error)
      ? '<div style="background:rgba(220,50,50,0.12);border:1px solid var(--color-accent-red);'
        + 'border-radius:6px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--color-accent-red)">'
        + '⚠ <strong>Env bootstrap failing for ' + esc(name) + ':</strong> '
        + esc(ec.last_bootstrap_error)
        + '</div>'
      : '';
    ecSection = '<div class="card" style="margin-bottom:14px">'
      + '<div style="font-weight:bold;margin-bottom:10px;font-size:13px">Env bootstrap</div>'
      + ecErrBanner
      + '<table style="font-size:12px;margin-bottom:10px"><tbody>' + ecRows + '</tbody></table>'
      + '<button class="action-btn" id="sym-env-bootstrap-btn" ' + disabled + '>Force bootstrap now</button>'
      + ' <span id="sym-env-bootstrap-msg" class="action-msg"></span>'
      + ' <button class="action-btn" id="sym-wiki-init-btn">Init wiki</button>'
      + ' <span id="sym-wiki-init-msg" class="action-msg"></span>'
      + '</div>';
  }
  el.innerHTML = backLink
    + '<h3 style="margin:12px 0 4px">' + esc(name) + '</h3>'
    + '<table data-detail style="font-size:12px;margin-bottom:18px"><tbody>' + statusRows + '</tbody></table>'
    + ecSection
    + '<div style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:12px;font-size:13px">Configuration</div>'
    + '<div style="margin-bottom:10px">'
    + '<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--color-text-primary);cursor:pointer">'
    + '<input id="sym-enabled" type="checkbox" ' + enabledChecked + '> Enabled</label>'
    + '</div>'
    + overrideInputs
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">Per-symphony persona overrides (leave blank to inherit global defaults)</div>'
    + personaInputs
    + '</div>'
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:6px">Env spec files (any change triggers a bootstrap)</div>'
    + '<div id="sym-spec-files-list" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">' + specFileTags + '</div>'
    + '<div style="display:flex;gap:6px">'
    + '<input id="sym-spec-file-input" type="text" placeholder="e.g. pyproject.toml" style="flex:1;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:4px 8px;font-size:12px">'
    + '<button type="button" class="action-btn" id="sym-spec-file-add-btn" style="padding:4px 10px;font-size:12px">Add</button>'
    + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:4px">'
    + '<button class="action-btn" id="sym-save-btn">Save</button>'
    + '<button class="action-btn" id="sym-delete-btn" style="background:var(--color-bg-delete);border-color:var(--color-border-delete);color:var(--color-accent-red)">Delete symphony</button>'
    + '<span id="sym-save-msg" class="action-msg"></span>'
    + '</div>'
    + '</div>';
  var ecBtn = document.getElementById('sym-env-bootstrap-btn');
  if (ecBtn) {
    ecBtn.addEventListener('click', async function() {
      var msg = document.getElementById('sym-env-bootstrap-msg');
      ecBtn.disabled = true;
      msg.textContent = 'Dispatching...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(name) + '/env-bootstrap', {method: 'POST'});
        var d = await r.json();
        if (r.ok) {
          msg.textContent = 'Accepted. Bootstrap will start on the next check';
          msg.style.color = 'var(--color-accent-green)';
          setTimeout(function() { loadSymphonyDetail(name, el); }, 1500);
        } else {
          msg.textContent = d.error || ('Error ' + r.status);
          msg.style.color = 'var(--color-accent-red)';
          ecBtn.disabled = false;
        }
      } catch(e) {
        msg.textContent = 'Network error';
        msg.style.color = 'var(--color-accent-red)';
        ecBtn.disabled = false;
      }
    });
  }
  var wikiBtn = document.getElementById('sym-wiki-init-btn');
  if (wikiBtn) {
    wikiBtn.addEventListener('click', async function() {
      var msg = document.getElementById('sym-wiki-init-msg');
      wikiBtn.disabled = true;
      msg.textContent = 'Dispatching...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(name) + '/wiki-init', {method: 'POST'});
        var d = await r.json();
        if (r.ok) {
          msg.textContent = 'Accepted — building the wiki, a seed PR will open on completion';
          msg.style.color = 'var(--color-accent-green)';
          setTimeout(function() { loadSymphonyDetail(name, el); }, 1500);
        } else {
          msg.textContent = d.error || ('Error ' + r.status);
          msg.style.color = 'var(--color-accent-red)';
          wikiBtn.disabled = false;
        }
      } catch(e) {
        msg.textContent = 'Network error';
        msg.style.color = 'var(--color-accent-red)';
        wikiBtn.disabled = false;
      }
    });
  }
  document.getElementById('sym-spec-file-add-btn').addEventListener('click', function() {
    var inp = document.getElementById('sym-spec-file-input');
    var val = inp.value.trim();
    if (!val) return;
    var list = document.getElementById('sym-spec-files-list');
    var tag = document.createElement('span');
    tag.setAttribute('data-spec-file', val);
    tag.setAttribute('style', 'display:inline-flex;align-items:center;gap:4px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:12px;padding:2px 8px;font-size:11px;margin:2px');
    tag.innerHTML = esc(val) + '<button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;cursor:pointer;color:var(--color-text-muted);font-size:13px;padding:0 0 0 2px;line-height:1">&times;</button>';
    list.appendChild(tag);
    inp.value = '';
  });
  document.getElementById('sym-save-btn').addEventListener('click', async function() {
    var msg = document.getElementById('sym-save-msg');
    var overrides = {};
    el.querySelectorAll('[data-override-key]').forEach(function(inp) {
      var k = inp.getAttribute('data-override-key');
      var v = inp.value.trim();
      if (v === '') return;
      var numFields = ['max_concurrent_cards','poll_interval_seconds','max_feedback_cycles','max_closed_pr_attempts_per_issue'];
      overrides[k] = numFields.includes(k) ? Number(v) : v;
    });
    var personas = {};
    el.querySelectorAll('[data-persona-role]').forEach(function(ta) {
      var role = ta.getAttribute('data-persona-role');
      var v = ta.value.trim();
      if (v) personas[role] = {instructions: v};
    });
    var specFilesList = [];
    el.querySelectorAll('[data-spec-file]').forEach(function(tag) {
      specFilesList.push(tag.getAttribute('data-spec-file'));
    });
    if (specFilesList.length === 0) specFilesList = ['README.md'];
    var enabled = document.getElementById('sym-enabled').checked;
    try {
      var r = await fetch('/api/symphonies/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: {'Content-Type':'application/json', 'If-Match': _symCfgHash || ''},
        body: JSON.stringify({overrides: Object.keys(overrides).length ? overrides : null, personas: Object.keys(personas).length ? personas : null, enabled: enabled, env_spec_files: specFilesList}),
      });
      var d = await r.json();
      if (r.ok) {
        _symCfgHash = r.headers.get('ETag') || _symCfgHash;
        msg.textContent = 'Saved';
        msg.style.color = 'var(--color-accent-green)';
      } else {
        msg.textContent = versionErrorText(r.status, d);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 4000);
  });
  document.getElementById('sym-delete-btn').addEventListener('click', async function() {
    if (!confirm('Delete symphony "' + name + '"? This cannot be undone.')) return;
    var msg = document.getElementById('sym-save-msg');
    try {
      var r = await fetch('/api/symphonies/' + encodeURIComponent(name), {
        method: 'DELETE',
        headers: {'If-Match': _symCfgHash || ''},
      });
      var d = await r.json();
      if (r.ok) {
        navigate(null, '/symphonies');
      } else {
        msg.textContent = versionErrorText(r.status, d);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
  });
}

async function submitAddSymphony() {
  var nameVal = (document.getElementById('sym-add-name') || {}).value || '';
  var projVal = (document.getElementById('sym-add-proj') || {}).value || '';
  var msg = document.getElementById('sym-add-msg');
  if (!nameVal.trim() || !projVal.trim()) { msg.textContent = 'Name and project number are required'; msg.style.color='var(--color-accent-red)'; return; }
  try {
    var r = await fetch('/api/symphonies', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'If-Match': (await currentConfigVersion()) || ''},
      body: JSON.stringify({name: nameVal.trim(), github_project_number: Number(projVal)}),
    });
    var d = await r.json();
    if (r.ok) {
      navigate(null, '/symphonies/' + encodeURIComponent(nameVal.trim()));
    } else {
      msg.textContent = versionErrorText(r.status, d);
      msg.style.color = 'var(--color-accent-red)';
    }
  } catch(e) { msg.textContent = 'Network error'; msg.style.color='var(--color-accent-red)'; }
}

// 081 config-editing UI: /static/config.js (#349)
function router() {
  var path = location.pathname;
  updateNavActive(path);
  if (isPerformersPath(path)) {
    showPage('performers-page');
    if (_lastState) renderPerformersPage(_lastState);
  } else if (path === '/personas') {
    showPage('personas-page');
    loadPersonasPage();
  } else if (path === '/history') {
    showPage('history-page');
    if (_lastState) renderHistoryPage(_lastState);
  } else if (path === '/symphonies' || path.startsWith('/symphonies/')) {
    showPage('symphonies-page');
    if (_lastState) renderSymphoniesPage(_lastState);
  } else if (path === '/glossary') {
    showPage('glossary-page');
    renderGlossaryPage();
  } else if (path === '/admin/config') {
    showPage('admin-config-page');
    loadGlobalConfigPage();
  } else if (path === '/config') {
    showPage('config-page');
    loadConfigPage();
  } else if (path === '/assistant') {
    showPage('assistant-page');
    if (typeof initAssistant === 'function') initAssistant();
  } else {
    showPage('dashboard-page');
    if (_lastState) renderDashboardExtras(_lastState);
  }
}

function navigate(e, path) {
  // Only intercept plain left-clicks; let ctrl/cmd/middle-click open new tabs normally
  if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0)) return;
  if (e) { e.preventDefault(); }
  history.pushState(null, '', path);
  window.closeMobileNav();
  router();
}


window.addEventListener('popstate', function() { router(); });

// 062: Swimlane view across symphonies — TODO / BLOCKED / IN_PROGRESS / IN_REVIEW.
// BACKLOG and DONE are intentionally hidden; the swimlane is meant to show only
// performers-page + active-work rendering: /static/performers.js (#349)
function renderHistoryPage(s) {
  renderCycleHistoryInto(document.getElementById('history-page-section'), s.cycle_history);
}

// 049: Personas page
var _personasPageLoaded = false;
function loadPersonasPage(force) {
  var section = document.getElementById('personas-page-section');
  if (!section) return;
  if (_personasPageLoaded && !force) return;  // skip if already loaded
  _personasPageLoaded = true;
  section.innerHTML = '<span class="empty-state">Loading...</span>';
  fetch('/api/personas').then(function(r){ _personaCfgHash = r.headers.get('ETag'); return r.json(); }).then(function(data) {
    var html = data.map(function(p, idx) {
      return '<div style="margin-bottom:16px" data-persona-idx="' + idx + '">' +
        '<label style="font-weight:bold;color:var(--color-text-primary);display:block;margin-bottom:4px" title="' + esc(p.role) + '">' + agentLabel(p.role) + personaStageNote(p.role) + '</label>' +
        '<textarea class="persona-page-ta" rows="4" style="width:100%;font-family:monospace;font-size:12px;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:8px;resize:vertical">' + esc(p.instructions || '') + '</textarea>' +
        '<div style="margin-top:4px;display:flex;gap:8px">' +
        '<button class="action-btn persona-save-btn">Save</button>' +
        '<button class="action-btn persona-reset-btn">Reset</button>' +
        '</div><div class="persona-page-msg action-msg"></div></div>';
    }).join('');
    section.innerHTML = html || '<span class="empty-state">No personas configured</span>';
    // Wire save/reset via event delegation (safe for all role names)
    section.querySelectorAll('[data-persona-idx]').forEach(function(el, i) {
      var role = data[i] && data[i].role;
      if (!role) return;
      var ta = el.querySelector('.persona-page-ta');
      var msg = el.querySelector('.persona-page-msg');
      el.querySelector('.persona-save-btn').addEventListener('click', async function() {
        if (!ta || !msg) return;
        try {
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'PUT', headers: {'Content-Type':'application/json', 'If-Match': _personaCfgHash || ''}, body: JSON.stringify({instructions: ta.value}) });
          if (res.ok) _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
          // The body has to be read: versionErrorText keys on `conflict` to tell a
          // version clash from a duplicate name, and passing null made every 409
          // read 'Error 409'.
          var pd = res.ok ? null : await res.json().catch(function(){ return {}; });
          msg.textContent = res.ok ? 'Saved' : versionErrorText(res.status, pd);
          setTimeout(function(){ msg.textContent=''; }, 3000);
        } catch(e) { msg.textContent = 'Error'; }
      });
      el.querySelector('.persona-reset-btn').addEventListener('click', async function() {
        if (!confirm('Reset ' + role + ' instructions to default?')) return;
        try {
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'DELETE', headers: {'If-Match': _personaCfgHash || ''} });
          if (res.ok) { loadPersonasPage(true); }
          else if (msg) {
            var rd = await res.json().catch(function(){ return {}; });
            msg.textContent = versionErrorText(res.status, rd);
          }
        } catch(e) { if (msg) msg.textContent = 'Error'; }
      });
    });
  }).catch(function() { section.innerHTML = '<span class="empty-state">Failed to load personas</span>'; });
}

// resetPersonaPage wired inline via event delegation in loadPersonasPage()

// 125: one timer refreshes every elapsed readout from its own start point.
setInterval(function() { refreshElapsedReadouts(document); refreshFreshness(); }, 1000);

async function forcePoll() {
  var btn = document.getElementById('force-poll-btn');
  var msg = document.getElementById('force-poll-msg');
  btn.disabled = true;
  msg.textContent = '';
  try {
    var res = await fetch('/api/force-poll', { method: 'POST' });
    if (res.status === 409) {
      msg.textContent = 'A check is already running';
      btn.disabled = false;
    }
    // 202: stay disabled until SSE delivers cycle_active=false
  } catch(err) {
    msg.textContent = 'Could not reach server';
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Personas panel (018-performer-personas)
// ---------------------------------------------------------------------------

async function loadPersonas() {
  var section = document.getElementById('personas-section');
  if (!section) return;
  try {
    var res = await fetch('/api/personas');
    _personaCfgHash = res.headers.get('ETag');
    if (!res.ok) {
      section.innerHTML = '<span class="empty-state">Could not load personas: ' + esc(res.status + ' ' + res.statusText) + '</span>';
      return;
    }
    var personas = await res.json();
    renderPersonas(personas);
  } catch(err) {
    section.innerHTML = '<span class="empty-state">Could not load personas: ' + esc(String(err)) + '</span>';
  }
}

function renderPersonas(personas) {
  var section = document.getElementById('personas-section');
  if (!section) return;
  var rows = personas.map(function(p) {
    var badge = p.is_default
      ? '<span style="font-size:11px;color:var(--color-text-muted);margin-left:6px">using default</span>'
      : '<span style="font-size:11px;color:var(--color-accent-blue);margin-left:6px">custom</span>';
    return '<div style="margin-bottom:16px;border-bottom:1px solid var(--color-bg-elevated);padding-bottom:14px">' +
      '<div style="display:flex;align-items:center;margin-bottom:6px">' +
        '<strong style="font-size:13px" title="' + esc(p.role) + '">' + agentLabel(p.role) + personaStageNote(p.role) + '</strong>' + badge +
      '</div>' +
      '<textarea id="persona-ta-' + esc(p.role) + '" rows="4" style="width:100%;box-sizing:border-box;' +
        'background:var(--color-bg-base);border:1px solid var(--color-border);color:var(--color-text-primary);padding:6px;font-family:monospace;' +
        'font-size:12px;border-radius:4px;resize:vertical">' + esc(p.instructions) + '</textarea>' +
      '<div style="margin-top:6px;display:flex;gap:8px;align-items:center">' +
        '<button onclick="savePersona(\'' + esc(p.role) + '\')" ' +
          'id="persona-save-' + esc(p.role) + '" ' +
          'style="background:var(--color-btn-success);border:none;color:var(--color-on-action);padding:4px 12px;border-radius:4px;cursor:pointer">' +
          'Save</button>' +
        '<button onclick="resetPersona(\'' + esc(p.role) + '\')" ' +
          'id="persona-reset-' + esc(p.role) + '" ' +
          'style="background:var(--color-bg-elevated);border:1px solid var(--color-border);color:var(--color-text-primary);padding:4px 12px;' +
          'border-radius:4px;cursor:pointer">Reset to defaults</button>' +
        '<span id="persona-msg-' + esc(p.role) + '" style="font-size:12px;color:var(--color-text-muted)"></span>' +
      '</div>' +
    '</div>';
  }).join('');
  section.innerHTML = rows || '<span class="empty-state">No personas found.</span>';
}

async function savePersona(role) {
  var ta = document.getElementById('persona-ta-' + role);
  var btn = document.getElementById('persona-save-' + role);
  var msg = document.getElementById('persona-msg-' + role);
  if (!ta || !btn) return;
  btn.disabled = true;
  msg.textContent = 'Saving...';
  try {
    var res = await fetch('/api/personas/' + encodeURIComponent(role), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'If-Match': _personaCfgHash || ''},
      body: JSON.stringify({instructions: ta.value}),
    });
    if (res.ok) _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
    var data = res.headers.get('content-type') && res.headers.get('content-type').includes('application/json')
      ? await res.json() : {};
    if (!res.ok) {
      msg.textContent = versionErrorText(res.status, data);
      msg.style.color = 'var(--color-accent-red)';
    } else {
      msg.textContent = 'Saved.';
      msg.style.color = 'var(--color-accent-green)';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = 'var(--color-text-muted)'; }}, 3000);
      loadPersonas();
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = 'var(--color-accent-red)';
  }
  btn.disabled = false;
}

async function resetPersona(role) {
  var btn = document.getElementById('persona-reset-' + role);
  var msg = document.getElementById('persona-msg-' + role);
  if (!btn) return;
  btn.disabled = true;
  msg.textContent = 'Resetting...';
  try {
    var res = await fetch('/api/personas/' + encodeURIComponent(role), {
      method: 'DELETE',
      headers: {'If-Match': _personaCfgHash || ''},
    });
    if (res.status === 204 || res.ok) {
      _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
      msg.textContent = 'Reset to defaults.';
      msg.style.color = 'var(--color-accent-green)';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = 'var(--color-text-muted)'; }}, 3000);
      loadPersonas();
    } else {
      var data = await res.json().catch(function() { return {}; });
      msg.textContent = versionErrorText(res.status, data);
      msg.style.color = 'var(--color-accent-red)';
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = 'var(--color-accent-red)';
  }
  btn.disabled = false;
}

// 049: Personas load lazily on /personas page visit via loadPersonasPage().
// The original loadPersonas() targets #personas-section on the main page (now hidden).
// The new loadPersonasPage() targets #personas-page-section and uses event delegation.

// ---------------------------------------------------------------------------
// 138: activity feed
// ---------------------------------------------------------------------------
var AF_MAX_ROWS = 2000;
var AF_SILENCE_MS = 40000;   // ~2.5 keepalive intervals (FR-026)
var AF_LABELS = {
  operator_action: 'OPERATOR', progress: 'PROGRESS', tool_use: 'TOOL', thinking: 'THINKING', cost: 'COST',
  stage_change: 'STAGE', recovered: 'RECOVERED', quiet: 'QUIET',
  stall: 'STALL', stuck: 'STUCK', error: 'ERROR', blocked: 'BLOCKED', completed: 'COMPLETED',
  // 327 added this activity type but only to AF_SUMMARIES, so the chip fell
  // back to the raw uppercased type while every other type had a short label.
  stream_truncated: 'TRUNCATED',
  // 343: step boundaries are the spine of the trail; give them their own chip.
  workflow_step: 'STEP',
  // 429: an observer verdict's chip. The verb itself is in the entry text.
  observer_verdict: 'OBSERVER'
};
var _afGroupingActive = false; // older SSE payloads lack session attribution
var _afEntries = [];        // retained entries, oldest-first
var _afSeen = {};           // seq -> true; reconnect backfill must not double-insert
var _afRunId = null;
var _afFilter = '';
var _afLastMsgAt = Date.now();
var _afLive = true;
var _afHasMessage = false;
var _afQuietCards = {};     // card_id -> true while that card is in a quiet episode

function afSyncRun(runId) {
  if (typeof runId !== 'string' || !runId || runId === _afRunId) return;
  _afRunId = runId;
  _afEntries = [];
  _afSeen = {};
  _afQuietCards = {};
  _afGroupingActive = false;
  var feed = document.getElementById('activity-feed');
  if (feed) feed.innerHTML = '';
  afUpdateEmpty();
  afUpdateFilterOptions();
}

function afLabel(t) { return AF_LABELS[t] || String(t || '').toUpperCase() || 'EVENT'; }

function afTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString(); } catch(e) { return ''; }
}


function afMatches(e) { return !_afFilter || e.card_id === _afFilter; }

function afAppend(entries) {
  var feed = document.getElementById('activity-feed');
  if (!feed || !entries || !entries.length) return;
  var html = '';
  var added = 0;
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    if (e == null || _afSeen[e.seq]) continue;
    if (e.session_id) _afGroupingActive = true;
    _afSeen[e.seq] = true;
    var prev = _afEntries[_afEntries.length - 1];
    e._afGroupKey = afCanGroup(prev, e) ? prev._afGroupKey : e.seq;
    _afEntries.push(e);
    // FR-032: any real entry ends the card's quiet episode.
    if (e.card_id && e.activity_type !== 'quiet') delete _afQuietCards[e.card_id];
    if (!afMatches(e)) continue;
    html = afRowHtml(e) + html;   // batch is oldest-first; newest ends up on top
    added++;
  }
  while (_afEntries.length > AF_MAX_ROWS) delete _afSeen[_afEntries.shift().seq];
  if (_afGroupingActive) {
    afSyncGroups(false);
    afUpdateEmpty();
    afUpdateFilterOptions();
    return;
  }
  if (added) {
    // Prepend ONLY the new rows. A full re-render would make
    // aria-relevant="additions" re-announce every existing row.
    feed.insertAdjacentHTML('afterbegin', html);
    while (_afEntries.length > AF_MAX_ROWS) delete _afSeen[_afEntries.shift().seq];
    while (feed.childElementCount > AF_MAX_ROWS) feed.removeChild(feed.lastElementChild);
  }
  afUpdateEmpty();
  afUpdateFilterOptions();
}

function afRender() {
  // Full re-render — filter changes only, never a live update.
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  if (_afGroupingActive) {
    afSyncGroups(true);
    afUpdateEmpty();
    return;
  }
  var html = '';
  for (var i = 0; i < _afEntries.length; i++) {
    if (afMatches(_afEntries[i])) html = afRowHtml(_afEntries[i]) + html;
  }
  feed.innerHTML = html;
  afUpdateEmpty();
}

function afUpdateEmpty() {
  var feed = document.getElementById('activity-feed');
  var empty = document.getElementById('af-empty');
  if (!feed || !empty) return;
  empty.style.display = feed.childElementCount ? 'none' : '';
}

function afUpdateFilterOptions() {
  var sel = document.getElementById('af-filter');
  if (!sel) return;
  var seen = {}, cards = [];
  for (var i = 0; i < _afEntries.length; i++) {
    var e = _afEntries[i];
    if (!e.card_id || seen[e.card_id]) continue;
    seen[e.card_id] = true;
    cards.push(e);
  }
  if (_afFilter && !seen[_afFilter]) cards.push({card_id:_afFilter, card_title:_afFilter + ' (no retained activity)'});
  var key = cards.map(function(e) { return e.card_id; }).join(',');
  if (key === sel.getAttribute('data-cards')) return;
  sel.setAttribute('data-cards', key);
  var html = '<option value="">All cards</option>';
  cards.forEach(function(e) {
    var label = (e.card_number ? '#' + e.card_number + ' ' : '') + (e.card_title || e.card_id);
    html += '<option value="' + esc(e.card_id) + '">' + esc(label) + '</option>';
  });
  sel.innerHTML = html;
  sel.value = _afFilter;
  if (sel.value !== _afFilter) { _afFilter = ''; sel.value = ''; afRender(); }
}

function onActivityFilterChange() {
  var sel = document.getElementById('af-filter');
  _afFilter = sel ? sel.value : '';
  afRender();
}

function updateConnectionStatus(label) {
  var status = document.getElementById('connection-status');
  if (!status) return;
  var text = document.getElementById('connection-label');
  if (text && text.textContent !== label) text.textContent = label;
  status.title = 'Dashboard updates: ' + label + '. Last message: ' +
    (_afHasMessage ? new Date(_afLastMsgAt).toLocaleTimeString() : 'none received yet');
  var dot = document.getElementById('nav-sse-dot');
  if (dot) { if (label === 'Live') dot.classList.remove('disconnected'); else dot.classList.add('disconnected'); }
}

function afSetLive(live) {
  if (live === _afLive) return;
  _afLive = live;
  var el = document.getElementById('af-liveness');
  if (el) {
    // 144: the header carries the one "Live" indicator; this one speaks up only when the feed is not live.
    el.textContent = live ? '' : 'Not live';
    if (live) el.classList.remove('af-not-live'); else el.classList.add('af-not-live');
  }
  // Keep retained entries visible on disconnect (FR-027).
  var note = document.getElementById('af-stale-note');
  if (note) note.style.display = live ? 'none' : '';
  if (!live) updateConnectionStatus('Updates quiet');
  var bnr = document.getElementById('disconnected-banner');
  if (bnr && !live) bnr.style.display = 'block';
}

function afNoteMessage() {
  _afLastMsgAt = Date.now();
  _afHasMessage = true;
  afSetLive(true);
  updateConnectionStatus('Live');
}

function afQuietTick() {
  var s = _lastState;
  if (!s) return;
  var threshold = s.activity_quiet_threshold_seconds;
  var sessions = (Array.isArray(s.active_sessions) ? s.active_sessions : []).filter(function(sess) {
    return !!sess.session_id && (sess.phase === 'monitoring_performer' || sess.phase === 'monitoring_agent');
  });
  var active = {};
  sessions.forEach(function(sess) { if (sess.card_id) active[sess.card_id] = true; });
  var marked = false;
  Object.keys(_afQuietCards).forEach(function(cid) {
    if (!active[cid] || !threshold || threshold <= 0) {
      delete _afQuietCards[cid];
      marked = true;
    }
  });
  if (!threshold || threshold <= 0) {
    if (marked) renderActivePerformers(s);
    return;   // 0 disables detection and clears existing markers
  }
  var now = Date.now();
  var newest = {};
  for (var i = 0; i < _afEntries.length; i++) {
    var e = _afEntries[i];
    if (!e.card_id || e.activity_type === 'quiet') continue;
    var t = Date.parse(e.timestamp);
    if (!isNaN(t) && (!newest[e.card_id] || t > newest[e.card_id])) newest[e.card_id] = t;
  }
  sessions.forEach(function(sess) {
    var cid = sess.card_id;
    if (!cid) return;
    // Anchor on max(newest entry, agent_dispatch_at), skipping whichever is
    // absent. Entry-only anchoring leaves a session restored after a daemon
    // restart unmarked forever — the exact card this exists for (FR-028).
    var lastHeard = newest[cid] || NaN;
    var dispatched = sess.agent_dispatch_at ? Date.parse(sess.agent_dispatch_at) : NaN;
    if (!isNaN(dispatched)) lastHeard = isNaN(lastHeard) ? dispatched : Math.max(lastHeard, dispatched);
    if (isNaN(lastHeard)) return;   // never mark quiet off a missing timestamp
    if ((now - lastHeard) / 1000 <= threshold) {
      // A fresh dispatch can replace a quiet worker without a phase change.
      if (_afQuietCards[cid]) { delete _afQuietCards[cid]; marked = true; }
      return;
    }
    if (_afQuietCards[cid]) return;
    _afQuietCards[cid] = true;
    marked = true;
    // One synthetic row per episode. Synthetic rows never enter the stored log
    // on the daemon and never count as progress (FR-031, FR-033).
    afAppend([{
      seq: 'q:' + cid + ':' + lastHeard,
      timestamp: new Date().toISOString(),
      card_id: cid,
      card_number: sess.issue_number,
      card_title: sess.card_title,
      stage: sess.performer_stage || '',
      activity_type: 'quiet',
      text: 'No activity for ' + Math.round((now - lastHeard) / 1000) + 's',
      truncated: false
    }]);
  });
  if (marked) renderActivePerformers(s);
}

function afTick() {
  afSetLive(Date.now() - _afLastMsgAt < AF_SILENCE_MS);
  afQuietTick();
}

var banner = document.getElementById('disconnected-banner');
var es;
var _sseRetryTimer = null;
var _sseRetryDelay = 1000;
function connectEventStream() {
  var source = new EventSource('/events');
  es = source;
  source.addEventListener('activity_event', function(e) {
    if (es !== source) return;
    afNoteMessage();
    try { afAppend(JSON.parse(e.data).entries); }
    catch(err) { console.error('activity parse error', err); }
  });
  source.addEventListener('heartbeat', function() { if (es === source) afNoteMessage(); });
  source.addEventListener('state_update', function(e) {
    if (es !== source) return;
    afNoteMessage();
    _lastStateAt = Date.now();
    try {
      _lastState = JSON.parse(e.data);
      // state_update precedes backfill on every stream connection.
      afSyncRun(_lastState.activity_run_id);
      renderState(_lastState);
      // 049: re-render current page with new state
      var path = location.pathname;
      if (isPerformersPath(path)) renderPerformersPage(_lastState);
      else if (path === '/history') renderHistoryPage(_lastState);
      else if (path === '/symphonies' || path.startsWith('/symphonies/')) renderSymphoniesPage(_lastState);
      else renderDashboardExtras(_lastState);
    } catch(err) { console.error('parse error', err); }
  });
  source.onerror = function() {
    if (es !== source) return;
    banner.style.display = 'block';
    updateConnectionStatus('Reconnecting');
    var fpBtn = document.getElementById('force-poll-btn');
    if (fpBtn) fpBtn.disabled = true;
    // Native EventSource retries CONNECTING, but HTTP failures can close it permanently.
    if (source.readyState === EventSource.CLOSED && _sseRetryTimer === null) {
      _sseRetryTimer = setTimeout(function() {
        _sseRetryTimer = null;
        if (es !== source || source.readyState !== EventSource.CLOSED) return;
        source.close();
        connectEventStream();
      }, _sseRetryDelay);
      _sseRetryDelay = Math.min(_sseRetryDelay * 2, 30000);
    }
  };
  source.onopen = function() {
    if (es !== source) return;
    if (_sseRetryTimer !== null) {
      clearTimeout(_sseRetryTimer);
      _sseRetryTimer = null;
    }
    _sseRetryDelay = 1000;
    banner.style.display = 'none';
    updateConnectionStatus('Waiting for updates');
  };
}
connectEventStream();

document.addEventListener('DOMContentLoaded', function() {
  router(); // initial route
  if (typeof assistantAvailable === 'function') assistantAvailable();
  setInterval(afTick, 5000);   // 138: silence timer + quiet detection
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && _selectedCardId) closeCardDetail();
  if (e.key === 'Escape' && document.getElementById('navbar-menu').classList.contains('open')) {
    toggleNavMenu(); document.getElementById('navbar-hamburger').focus();
  }
});
