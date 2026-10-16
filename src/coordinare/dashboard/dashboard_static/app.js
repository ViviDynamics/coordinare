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
  var secs = sess.workflow_step_entered_at
    ? Math.floor((Date.now() - new Date(sess.workflow_step_entered_at).getTime()) / 1000) : 0;
  // 0 means the operator has not said what "too long" is, so say nothing.
  var slow = slowAfterSeconds > 0 && secs > slowAfterSeconds;
  var style = slow ? 'color:var(--color-degraded)' : 'color:var(--color-accent-blue)';
  parts.push('<span style="' + style + ';font-weight:bold" data-step-since="'
    + esc(sess.workflow_step_entered_at || '') + '">&bull; ' + esc(shortName(cur))
    + (sess.workflow_step_entered_at ? ' ' + timeHtml(sess.workflow_step_entered_at, '') : '') + '</span>');
  var more = done.length > 6 ? '<span class="muted">+' + String(done.length - 6) + ' earlier &middot; </span>' : '';
  return '<div class="step-trail" style="margin-top:4px;font-size:12px">' + more + parts.join('<span class="muted"> &middot; </span>') + '</div>';
}

// humanPhase + formatPhaseLabel: /static/helpers.js (#349)
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
        '<td>' + timeHtml(r.timestamp) + '</td>' +
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
    // 139: an item on a card no symphony claims is not the conductor's to describe.
    if (sessions[i].unclaimed) continue;
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
  // 154: the headline is the status strip's text (layout.js). Its description and the
  // conductor's internal state are the tooltip.
  var phaseEl = document.getElementById('phase');
  var phaseDescriptions = {
    'idle':             'Waiting for a card to enter the TODO column on the GitHub Project.',
    'running':          'Running a check.',
    'blocked':          'Needs human: the card is stopped until a person acts. Review the Open Questions below.',
    'dispatching':      'Assessing and dispatching card to the performer agent.',
    'monitoring_agent': 'Performer agent is actively working on the card.',
    'monitoring_performer': 'Performer agent is actively working on the card.',
    'monitoring_pr':    'Waiting for PR review approval.',
    'merging':          'Merging the approved pull request.',
    'relay_feedback':   'Relaying PR review feedback to the performer agent.',
    'recovery':         'A check failed; the daemon is recovering before retrying.',
  };
  phaseEl.title = 'Conductor state: ' + String(effectivePhase || '');
  // 154 review: the description is visible text in the strip (truncated, full text on hover).
  var descEl = document.getElementById('phase-desc');
  var description = phaseDescriptions[effectivePhase] || '';
  if (descEl) {
    descEl.textContent = description;
    descEl.title = description;
  }
  phaseEl.className = 'phase ' + phaseClass(effectivePhase);
  renderStatusStrip(s, activeSess);

  // Flowchart (async)
  updateFlowChart(s.phase);

  // Cards (the board). The active runs are rendered by renderDashboardExtras.
  renderActiveWorkPanels(s);
  markBoardOverflow();
  var cardsSummary = document.getElementById('cards-summary');
  if (cardsSummary) cardsSummary.textContent = boardCountsText(s);

  // T032: Live-update card detail view if one is open
  if (_selectedCardId) {
    var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
    var activeSess = sessions.find(function(ss) { return ss.card_id === _selectedCardId; });
    if (activeSess) {
      var detailContent = document.getElementById('card-detail-content');
      if (detailContent) { detailContent.innerHTML = renderCardDetailContent(activeSess, s); hydrateCardRuns(detailContent); }
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
    depCard.innerHTML = '<h3>Waiting on Dependencies</h3><ul id="dependency-blockers-list"></ul>';
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
      if (d.title) label += ': ' + esc(d.title);
      var col = d.column || 'not on the GitHub Project';
      var link = d.issue_url && /^https?:\/\//i.test(d.issue_url)
        ? '<a href="' + esc(d.issue_url) + '" target="_blank" rel="noopener">' + esc(label) + '</a>'
        : esc(label);
      return '<li>' + link + ' <span style="opacity:0.7">(' + esc(col) + ')</span></li>';
    }).join('');
  } else {
    depCard.style.display = 'none';
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
      var age = obj.asked_at ? ' <span class="muted">asked ' + timeHtml(obj.asked_at) + '</span>' : '';
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
      // 142: a rebase that needs a person reads as such, not as the bare outcome code.
      var outcomeText = j.outcome === 'blocked' ? 'needs human' : j.outcome;
      var label = emoji + ' ' + esc(j.branch.split('/').pop() || j.branch) + ': ' + esc(outcomeText);
      if (sha) label += ' (' + esc(sha) + ')';
      // 139: a branch that needs a person names its symphony, or says it covers all of them.
      if (j.unattributed) label += ' · covers all symphonies';
      else if (j.symphony) label += ' · ' + esc(j.symphony);
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
      var header = 'Round ' + (i + 1) + (attr ? ': ' + attr : '');
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
  daemonStartEl.innerHTML = timeHtml(s.daemon_start_time);
  // 134/154: the card's tokens and cost are on its active-run tile (layout.js); the
  // spend figures are in the status strip.
  renderSpend(s.token_usage);
  // Current run (141: the run's short name, with its full id in the title and a Copy button)
  // 154: a run that an active-run tile already names is not repeated here.
  var agentEl = document.getElementById('agent-session');
  var tiled = activeRunSessions(s).some(function(sess) { return sess.session_id === s.agent_session_id; });
  if (s.agent_session_id && !tiled) {
    agentEl.style.display = '';
    document.getElementById('agent-session-id').innerHTML = runIdHtml(s.agent_session_id, {copy: true});
  } else {
    agentEl.style.display = 'none';
  }

  // Subsystems
  var subsEl = document.getElementById('subsystems-section');
  if (s.system_health) {
    // 151: one row per subsystem, with its status and how fresh it is.
    subsEl.innerHTML = systemHealthHtml(s.system_health);
    // 151: the user's own open or closed choice is kept across re-renders (see systemHealthOpen).
    var shCard = subsEl.querySelector('details');
    if (shCard) shCard.addEventListener('toggle', function() { systemHealthRemember(shCard.open); });
  } else if (!s.subsystems || s.subsystems.length === 0) {
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

  // 154: the cycle log is on the History page (its Recent checks panel), not the Dashboard.
  renderCycleHistoryInto(document.getElementById('history-cycles'), s.cycle_history, { collapse: true });
  // 139: the panels that are not known per symphony say so, last, so they win over the rows above.
  applyScopeNotes(s);
}

// esc + fmtBytes: /static/helpers.js (#349)

var _selectedCardId = null;   // card_id shown in card-detail-view

// parseTokenCount + derivePerformerTokenTotal: /static/performers.js (#349)
// Refresh session age counter every 10s while monitoring_agent
var _lastState = null;

// 049: Client-side router
function showPage(pageId) {
  var pages = ['dashboard-page','performers-page','personas-page','history-page','logs-page','glossary-page','symphonies-page','config-page','assistant-page','runs-page','card-page','bootstrap-page'];
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
  var titles = {'/':'Dashboard | Coordinare','/performers':'Performers | Coordinare','/personas':'Personas | Coordinare','/logs':'Logs | Coordinare','/history':'History | Coordinare','/glossary':'Glossary | Coordinare','/symphonies':'Symphonies | Coordinare','/config':'Settings | Coordinare'};
  var activeLink = document.querySelector('.nav-link.nav-active');
  // 153: the label, not the link's text: a needs-attention badge sits in the link.
  var activeLabel = activeLink ? activeLink.querySelector('.nav-link-label') || activeLink : null;
  document.getElementById('workspace-title').textContent = activeLabel ? activeLabel.textContent.trim() : 'Dashboard';
  // 153: setPageTitle keeps the "(N)" attention prefix across navigation.
  setPageTitle(titles[path] || (path.startsWith('/symphonies/') ? 'Symphony | Coordinare' : path.indexOf('/performers/') === 0 ? 'Run detail | Coordinare' : isRunsPath(path) ? 'Transcript | Coordinare' : isCardPath(path) ? 'Card | Coordinare' : 'Coordinare'));
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
    if (tbl && sym && _symShown === detail) {
      var rows = tbl.querySelectorAll('tbody tr');
      function _setTd(row, val) { if (row) { var td = row.querySelector('td'); if (td) td.textContent = val; } }
      _setTd(rows[1], sym.cycle_count != null ? sym.cycle_count : 'None');
      _setTd(rows[2], sym.error_count != null ? sym.error_count : 'None');
      _setTd(rows[3], sym.state && sym.state.active_card ? (sym.state.active_card.title || sym.state.active_card.id || '') : 'None');
      var lastPollCell = rows[4] ? rows[4].querySelector('td') : null;
      if (lastPollCell) lastPollCell.innerHTML = sym.last_poll_at ? timeHtml(sym.last_poll_at) : 'None';
      var personas = el.querySelector('[data-persona-context]');
      if (personas) personaSelect(personas, personaHashRole('symphony-read'), false);
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
  var choice = currentSymphonyChoice(s);
  var rows = syms.map(function(sym) { return symphonyTableRowHtml(sym, choice); }).join('');
  el.innerHTML = '<div style="overflow-x:auto"><table style="width:100%;font-size:12px"><thead><tr>'
    + '<th style="text-align:left">Name</th>'
    + '<th style="text-align:left" title="Whether coordinare checks this symphony, and why not.">Status</th>'
    + '<th style="text-align:left" title="The GitHub Project this symphony works from.">GitHub Project</th>'
    + '<th style="text-align:left" title="The GitHub repository this symphony works against.">Repository</th>'
    + '<th style="text-align:left" title="Whether the symphony\'s performer environment is cached.">Env</th>'
    + '<th style="text-align:left" title="Cards a performer is working on now.">Cards active</th>'
    + '<th style="text-align:left" title="Cards waiting for a performer slot.">Cards queued</th>'
    + '<th style="text-align:left" title="Cost this week (UTC) from the transcript ledger.">Spend this week</th>'
    + '<th style="text-align:left" title="Checks of the board this symphony has run.">Checks run</th>'
    + '<th style="text-align:left" title="Checks that failed in a row. A successful check resets this to 0.">Failed checks in a row</th>'
    + '<th style="text-align:left" title="When coordinare last checked this symphony.">Last checked</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    + '<div id="sym-list-msg" class="action-msg" style="margin-top:8px"></div>'
    + '<div style="margin-top:14px"><button class="action-btn" onclick="document.getElementById(\'sym-add-form\').style.display=\'block\'">+ Add Symphony</button></div>'
    + addFormHtml;
  document.getElementById('sym-list-msg').textContent = bootstrapListMessage();
  el.querySelectorAll('.sym-row-bootstrap, .sym-row-bootstrap-cancel').forEach(function(b) {
    b.addEventListener('click', function() {
      var n = b.getAttribute('data-sym');
      var msg = document.getElementById('sym-list-msg');
      bootstrapAction(n, b.classList.contains('sym-row-bootstrap-cancel'), b, msg);
    });
  });
  // 156: a click anywhere on a row opens that symphony's page. The name link and the Bootstrap
  // button keep their own clicks.
  el.querySelectorAll('tr.symphony-row').forEach(function(tr) {
    tr.addEventListener('click', function(e) {
      // 266: a disclosure in the row (the bootstrap history) opens itself; it does not open the symphony.
      if (e.target && e.target.closest && e.target.closest('a,button')) return;
      if (e.target && e.target.closest && e.target.closest('summary,details')) return;
      navigate(e, tr.getAttribute('data-href'));
    });
  });
}

async function loadSymphonyDetail(name, el) {
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var backLink = '<a href="/symphonies" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue);font-size:13px">&#8592; All symphonies</a>';
  var res, data;
  try {
    res = await fetch('/api/symphonies/' + encodeURIComponent(name));
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
  var statusRows = [
    ['GitHub project', data.github_project_number != null ? '#' + data.github_project_number : 'None'],
    ['Cycles', st.cycle_count != null ? st.cycle_count : 'None'],
    ['Failed checks in a row', st.error_count != null ? st.error_count : 'None'],
    ['Active card', st.active_card ? esc(st.active_card.title || st.active_card.id || '') : 'None'],
    ['Last checked', st.last_poll_at ? timeHtml(st.last_poll_at) : 'None'],
    ['Last error', st.last_error ? esc(st.last_error) : 'None'],
  ].map(function(r) {
    return '<tr><th style="text-align:left;padding:3px 10px 3px 0;color:var(--color-text-muted);font-weight:normal;white-space:nowrap">' + r[0] + '</th><td style="font-size:12px">' + r[1] + '</td></tr>';
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
          + esc(tryCountText(ec.last_inference_attempts || '?')) + ', '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else if (ec.last_inference_succeeded === false) {
        infRow = '<span style="color:var(--color-accent-red)">failed</span> '
          + '<span style="color:var(--color-text-muted)">('
          + esc(tryCountText(ec.last_inference_attempts || '?')) + ', '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else {
        infRow = 'None';
      }
      ecRows = [
        ['Status', '<span style="color:' + statusColor + '">' + statusText + '</span>'],
        ['Performer', esc(ecBootstrapId)],
        ['Cache dir', esc(ec.cache_dir || 'None')],
        ['Cache ready', ready ? 'yes' : 'no'],
        ['Bootstrap in flight', inFlight ? 'yes' : 'no'],
        ['Last bootstrap', ec.last_bootstrap_at ? timeHtml(ec.last_bootstrap_at) : 'None'],
        ['Recorded SHA', ec.readme_sha ? esc(ec.readme_sha) : 'None'],
        ['Service inference', infRow],
        ['Inference at', ec.last_inference_at ? timeHtml(ec.last_inference_at) : 'None'],
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
    + symSummaryHtml(name, data);
  loadSymphonyPersonas(name, data, el);
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
          msg.textContent = 'Accepted: building the wiki, a seed PR will open on completion';
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
  _symShown = name;
}

// 221: the symphony's Configuration form uses the Settings editors' safeguards: the review before a
// save, the lock while it is open, the unsaved-changes guard and the conflict reapply (config.js).
// Its controls carry data-field like a Settings control, so the shared functions read and restore them.
// The state it reads is the symphony GET the page loaded, never a copy it keeps elsewhere.
var _symBase = {overrides: {}, personas: {}};
// The symphony whose status summary is drawn, refreshed on a new visit.
var _symShown = null;

function symOverrideFields() {
  return [
    {key: 'max_concurrent_cards', label: 'Max concurrent cards', type: 'number', placeholder: '1'},
    {key: 'max_cards_in_review', label: 'Max cards waiting on review', type: 'number', placeholder: '3', noCapOption: true},
    {key: 'poll_interval_seconds', label: 'Poll interval (seconds)', type: 'number', placeholder: '30'},
    {key: 'max_feedback_cycles', label: 'Max feedback cycles', type: 'number', placeholder: '5'},
    {key: 'max_closed_pr_attempts_per_issue', label: 'Max closed PRs per issue', type: 'number', placeholder: '3'},
    {key: 'assignee_filter', label: 'Assignee filter (GitHub login)', type: 'text', placeholder: '(no filter)'},
  ];
}

// 221: one override. A number that a No cap box switches off takes its value from the box, so the box
// is a field of its own and both are read by the shared functions.
function symOverrideRow(f, ov) {
  var field = 'overrides.' + f.key;
  var has = Object.prototype.hasOwnProperty.call(ov, f.key);
  var noCap = !!f.noCapOption && has && ov[f.key] === null;
  var shown = has && ov[f.key] !== null && !noCap ? ov[f.key] : '';
  var inputId = 'sym-ov-' + f.key;
  var input = '<input id="' + esc(inputId) + '" type="' + (f.type === 'number' ? 'number' : 'text') + '"'
    + ' data-override-key="' + esc(f.key) + '" data-field="' + esc(field) + '"'
    + ' data-type="' + (f.type === 'number' ? 'int' : 'text') + '" data-label="' + esc(f.label) + '"'
    + ' value="' + esc(String(shown)) + '" placeholder="' + esc(f.placeholder) + '"'
    + (noCap ? ' disabled' : '') + ' style="' + CFG_INPUT + '">';
  var box = f.noCapOption
    ? '<label style="font-size:11px;color:var(--color-text-muted);display:inline-flex;align-items:center;gap:4px;margin-top:4px">'
      + '<input id="sym-nocap-' + esc(f.key) + '" type="checkbox" data-override-nocap="' + esc(f.key) + '"'
      + ' data-off-target="' + esc(inputId) + '" data-field="' + esc(field + '.nocap') + '"'
      + ' data-type="bool" data-label="' + esc(f.label + ': no cap') + '"' + (noCap ? ' checked' : '')
      + '> No cap (unlimited)</label>'
      + '<div style="font-size:11px;color:var(--color-text-muted);margin-top:2px">Empty uses the global default.</div>'
    : '';
  return '<div style="margin-bottom:8px">'
    + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(f.label) + '</label>'
    + input + box + '</div>';
}

// 221: a secret override is typed into, never shown. The server holds the value and shows the mask, so
// the page has nothing to write into the input, and Show reveals only what is typed.
// 221: Clear is offered only where the global field may be empty (#225's required rule, from the server).
function symSecretRow(secret) {
  var key = secret.key;
  var clear = secret.clearable
    ? '<button type="button" class="cfg-btn" data-cfg-action="clear-secret" data-clear-for="' + esc('overrides.' + key) + '">Clear</button>'
    : '';
  return '<div style="margin-bottom:8px">'
    + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(key) + ' (secret)</label>'
    + '<div style="display:flex;gap:6px;align-items:center">'
    + '<input type="password" autocomplete="off" data-secret="true" data-field="' + esc('overrides.' + key) + '"'
    + ' data-type="text" data-label="' + esc(key) + '" placeholder="leave masked to keep current" style="' + CFG_INPUT + '">'
    + '<button type="button" class="cfg-btn" data-cfg-action="toggle-secret" aria-pressed="false">Show</button>'
    + clear
    + '</div></div>';
}

function symSpecFileChip(file) {
  return '<span data-spec-file="' + esc(file) + '" style="display:inline-flex;align-items:center;gap:4px;background:var(--color-bg-elevated);'
    + 'border:1px solid var(--color-border);border-radius:12px;padding:2px 8px;font-size:11px;margin:2px">'
    + esc(file)
    + '<button type="button" class="cfg-btn" data-cfg-action="remove-spec-file" aria-label="' + esc('Remove ' + file) + '"'
    + ' style="background:none;border:none;cursor:pointer;color:var(--color-text-muted);font-size:13px;padding:0 0 0 2px;line-height:1">'
    + '&times;</button></span>';
}

// 221: the Configuration card. Every control it shows is a data-field the shared functions read.
function symFormHtml(name, data, catalog) {
  var ov = data.overrides || {};
  var specFiles = data.env_spec_files || ['README.md'];
  var secretKeys = data.secret_overrides || [];
  var enabledChecked = data.enabled !== false ? ' checked' : '';
  var overrideRows = symOverrideFields().map(function(f) { return symOverrideRow(f, ov); }).join('');
  var secretRows = secretKeys.map(function(secret) { return symSecretRow(secret); }).join('');
  var chips = specFiles.map(function(file) { return symSpecFileChip(file); }).join('');
  return '<div id="sym-config-form" data-item data-scope="' + esc('symphony:' + name) + '" data-symphony="' + esc(name)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<p><a href="/symphonies" data-cfg-action="navigate" data-cfg-href="/symphonies">All symphonies</a></p>'
    + '<h3>' + esc(name) + ' settings <span data-sym-dirty hidden>Unsaved</span></h3>'
    + '<div style="margin-bottom:10px">'
    + '<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--color-text-primary);cursor:pointer">'
    + '<input id="sym-enabled" type="checkbox" data-field="enabled" data-type="bool" data-label="Enabled"' + enabledChecked + '> Enabled</label>'
    + '</div>'
    + overrideRows
    + secretRows
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + symPersonasHtml(name, data.personas || {}, catalog, true)
    + '</div>'
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:6px">Env spec files (any change triggers a bootstrap)</div>'
    + '<div id="sym-spec-files-list" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">' + chips + '</div>'
    + '<input type="hidden" data-field="env_spec_files" data-type="list" data-label="Env spec files" value="' + esc(specFiles.join(', ')) + '">'
    + '<div style="display:flex;gap:6px">'
    + '<input id="sym-spec-file-input" type="text" placeholder="e.g. pyproject.toml" style="' + CFG_INPUT + ';flex:1">'
    + '<button type="button" class="action-btn" data-cfg-action="add-spec-file" style="padding:4px 10px;font-size:12px">Add</button>'
    + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:4px">'
    + '<button type="button" class="action-btn" id="sym-save-btn" data-cfg-action="save-symphony">Save</button>'
    + '<button type="button" class="action-btn" id="sym-delete-btn" data-cfg-action="delete-symphony"'
    + ' style="background:var(--color-bg-delete);border-color:var(--color-border-delete);color:var(--color-accent-red)">Delete symphony</button>'
    + '<span id="sym-save-msg" data-status class="action-msg"></span>'
    + '</div>'
    + '</div>';
}

// 221: the body a symphony save sends, built from the same entries the review shows. It starts from the
// symphony as the server holds it, so a setting this form does not show is kept. Each shown field
// replaces its own key or removes it. A secret the reader did not type keeps its mask, which the server
// restores from what it holds. Pure, so the node check runs it as it ships.
function symCopy(source) {
  var out = {};
  Object.keys(source || {}).forEach(function(key) { out[key] = source[key]; });
  return out;
}

function symRequestBody(entries, base) {
  var overrides = symCopy(base.overrides);
  var personas = symCopy(base.personas);
  var noCap = {};
  var enabled = true;
  var specFiles = ['README.md'];
  entries.forEach(function(e) {
    var key = e.key;
    if (key === 'enabled') { enabled = e.after !== false; return; }
    if (key === 'env_spec_files') { if (Array.isArray(e.after) && e.after.length) specFiles = e.after; return; }
    if (key.indexOf('personas.') === 0) {
      var role = key.slice('personas.'.length);
      var text = String(e.after === null || e.after === undefined ? '' : e.after);
      if (text.trim()) {
        var persona = symCopy(personas[role]);
        persona.instructions = text;
        personas[role] = persona;
      }
      else delete personas[role];
      return;
    }
    if (key.indexOf('overrides.') !== 0) return;
    var name = key.slice('overrides.'.length);
    if (name.slice(-6) === '.nocap') { noCap[name.slice(0, -6)] = e.after === true; return; }
    if (e.secret) {
      // A cleared secret is written absent. A typed one replaces the stored value, and an untouched one stays masked.
      if (e.cleared) delete overrides[name];
      else if (cfgSecretChanged(e.after)) overrides[name] = e.after;
      return;
    }
    var value = typeof e.after === 'string' ? e.after.trim() : e.after;
    if (value === null || value === undefined || value === '') delete overrides[name];
    else overrides[name] = value;
  });
  Object.keys(noCap).forEach(function(name) { if (noCap[name]) overrides[name] = null; });
  return {
    overrides: Object.keys(overrides).length ? overrides : null,
    personas: Object.keys(personas).length ? personas : null,
    enabled: enabled,
    env_spec_files: specFiles,
  };
}

// 221: Save. The snapshot is taken now: the review shows it, and the confirmed request sends it.
function symSave(btn) {
  var form = btn.closest('[data-item]');
  var statusEl = form.querySelector('[data-status]');
  var name = form.getAttribute('data-symphony') || '';
  cfgClearErrors(form);
  var entries = cfgEntries(form);
  var changed = entries.filter(cfgChanged);
  if (!changed.length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  var body = symRequestBody(entries, _symBase);
  var pending = cfgPendingFor(form, changed);
  cfgShowReview(form, cfgDiffRows(changed), function() {
    return symValidateAndSave(name, body, pending, form, statusEl, btn);
  });
}

// 221: Delete goes through the same review as every other change, and the same conflict check.
function symDelete(btn) {
  var form = btn.closest('[data-item]');
  var statusEl = form.querySelector('[data-status]');
  var name = form.getAttribute('data-symphony') || '';
  var rows = [{label: 'Symphony', before: name, after: '(deleted)', secret: false, cleared: false}];
  cfgShowReview(form, rows, function() {
    _cfgPending = null;
    cfgPut('/api/symphonies/' + encodeURIComponent(name),
      {store: 'symphony', _method: 'DELETE', 'If-Match': _symCfgHash || ''}, form, statusEl, btn);
  });
}

function symSyncSpecFiles(form) {
  var names = Array.prototype.map.call(form.querySelectorAll('#sym-spec-files-list [data-spec-file]'),
    function(chip) { return chip.getAttribute('data-spec-file'); });
  form.querySelector('input[data-field="env_spec_files"]').value = names.join(', ');
}

function symAddSpecFile(btn) {
  var form = btn.closest('[data-item]');
  var input = form.querySelector('#sym-spec-file-input');
  var statusEl = form.querySelector('[data-status]');
  var value = input.value.trim();
  if (!value) return;
  // The list is saved comma-separated, so a name with a comma would be read as two names.
  if (value.indexOf(',') !== -1) { cfgFlash(statusEl, 'A file name cannot contain a comma', 'red'); return; }
  var present = Array.prototype.some.call(form.querySelectorAll('#sym-spec-files-list [data-spec-file]'),
    function(chip) { return chip.getAttribute('data-spec-file') === value; });
  if (!present) {
    form.querySelector('#sym-spec-files-list').insertAdjacentHTML('beforeend', symSpecFileChip(value));
    symSyncSpecFiles(form);
  }
  input.value = '';
}

function symRemoveSpecFile(btn) {
  var form = btn.closest('[data-item]');
  var chip = btn.closest('[data-spec-file]');
  if (chip && chip.parentNode) chip.parentNode.removeChild(chip);
  symSyncSpecFiles(form);
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
// 143: the route the page last showed. A declined leave (unsaved Settings) goes back here.
var _lastRoute = '';
// 143 review: every entry the page creates carries a sequence number. A declined Back
// then returns to the exact entry it left with history.go, and adds no entry.
var _navSeq = 0;
var _restoringRoute = false;
function tagRouteEntry() {
  var state = history.state;
  if (state && typeof state.seq === 'number') { _navSeq = state.seq; return; }
  history.replaceState({seq: _navSeq}, '', location.href);
}
function pushRoute(url) {
  _navSeq += 1;
  history.pushState({seq: _navSeq}, '', url);
  _lastRoute = location.pathname + location.search + location.hash;
}
// 143: the Global Config page is the General tab of Settings. Its old address
// moves there, keeping the query string.
function redirectRetiredPages() {
  if (location.pathname !== '/admin/config') return;
  // 143 second review: the redirected entry keeps a sequence number, so a later Back can return to it exactly.
  history.replaceState({seq: _navSeq}, '', '/config' + location.search + '#global');
}
function router() {
  tagRouteEntry();
  redirectRetiredPages();
  var path = location.pathname;
  // 143: leaving Settings ends its rendered state, so the next visit loads it fresh.
  if (path !== '/config') _cfgShown = false;
  if (path !== '/personas') _personasPageLoaded = false;
  // 221: a symphony's form is drawn again when its page is entered again, so a discarded edit does not come back.
  if (path !== '/symphonies' && path.indexOf('/symphonies/') !== 0) _symShown = null;
  updateNavActive(path);
  // 139: the header control follows the page, and a deep link is applied on arrival.
  renderSymphonySwitcher();
  if (isPerformersPath(path)) {
    showPage('performers-page');
    if (_lastState) renderPerformersPage(scopedView(_lastState));
  } else if (path === '/personas') {
    showPage('personas-page');
    loadPersonasPage();
  } else if (path === '/logs') {
    showPage('logs-page');
    renderLogsPage(currentScopedState(), true);
  } else if (path === '/history') {
    showPage('history-page');
    var historyNow = currentScopedState();
    if (historyNow) renderHistoryPage(historyNow);
  } else if (isBootstrapRunPath(path)) {
    showPage('bootstrap-page');
    bootstrapPageRefresh();
  } else if (path === '/symphonies' || path.startsWith('/symphonies/')) {
    showPage('symphonies-page');
    if (_lastState) renderSymphoniesPage(_lastState);
  } else if (path === '/glossary') {
    showPage('glossary-page');
    renderGlossaryPage();
  } else if (path === '/config') {
    showPage('config-page');
    if (_cfgShown) cfgSyncTab(); else loadConfigPage();
  } else if (path === '/assistant') {
    showPage('assistant-page');
    if (typeof initAssistant === 'function') initAssistant();
  } else if (isRunsPath(path)) {
    // 133: the transcript viewer. Its own address, so Back and reload both work.
    showPage('runs-page');
    runsPageRoute(path, location.search);
  } else if (isCardPath(path)) {
    // 233: the card page. Its own address, so Back, Forward and reload all work.
    showPage('card-page');
    cardPageRoute(path);
  } else {
    showPage('dashboard-page');
    if (_lastState) renderDashboardExtras(scopedView(_lastState));
  }
  _lastRoute = location.pathname + location.search + location.hash;
}

function navigate(e, path) {
  // Only intercept plain left-clicks; let ctrl/cmd/middle-click open new tabs normally
  if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0)) return;
  // 143: unsaved Settings edits are not dropped silently by an in-app link.
  if (!cfgLeaveAllowed(path.split('#')[0].split('?')[0])) {
    if (e) { e.preventDefault(); }
    return;
  }
  if (e) { e.preventDefault(); }
  // 139: in-app navigation keeps the symphony the header has chosen.
  pushRoute(pathWithSymphony(path));
  window.closeMobileNav();
  router();
}


window.addEventListener('popstate', function(event) {
  // The restore below is our own step back; it is not a new decision to make.
  if (_restoringRoute) { _restoringRoute = false; return; }
  var target = event.state && typeof event.state.seq === 'number' ? event.state.seq : null;
  // 143: Back and Forward leave a page with unsaved Settings edits only if the reader agrees.
  // A declined move steps back to the entry it came from: the exact URL, hash included.
  if (!cfgLeaveAllowed(location.pathname)) {
    if (target !== null && target !== _navSeq) {
      _restoringRoute = true;
      history.go(_navSeq - target);
    }
    return;
  }
  if (target !== null) _navSeq = target;
  router();
});

// 062: Swimlane view across symphonies — TODO / BLOCKED / IN_PROGRESS / IN_REVIEW.
// BACKLOG and DONE are intentionally hidden; the swimlane is meant to show only
// performers-page + active-work rendering: /static/performers.js (#349)
// 150: the page itself (the run table, its filters, the checks) is history.js. The
// Dashboard's Recent checks panel keeps the snapshot's cycle rows.

// 049: Personas page. 143: read-only. Persona text is edited in Settings, under Agents,
// so each persona has exactly one editor. Each entry links to its Settings item.
var _personasPageLoaded = false;

// 157: who made a persona's latest change, as the history records it. Nothing is invented: a blank or
// "unknown" author (a hand edit the dashboard did not see, or the first recorded text) reads as unknown.
function personaAuthorText(changedBy) {
  var who = String(changedBy || '');
  if (who === '' || who === 'unknown') return 'unknown author';
  if (who === 'local') return 'local operator';
  if (who === 'token') return 'shared token';
  return who;
}

// 157: "Last edited <age> by <author>" from a persona's version history (spec 130), or "not recorded".
function personaLastEditedHtml(history) {
  var versions = history && Array.isArray(history.versions) ? history.versions : [];
  var newest = null;
  for (var i = 0; i < versions.length; i++) {
    if (newest === null || Number(versions[i].version) > Number(newest.version)) newest = versions[i];
  }
  if (newest === null) return '<span class="muted">Last edited: not recorded</span>';
  return '<span class="muted">Last edited ' + timeHtml(newest.changed_at, 'at an unknown time') + ' by ' +
    esc(personaAuthorText(newest.changed_by)) + '</span>';
}

function loadPersonasPage(force) {
  var section = document.getElementById('personas-page-section');
  if (!section) return;
  if (_personasPageLoaded && !force) {
    var existing = section.querySelector('[data-persona-context]');
    if (existing) personaSelect(existing, personaHashRole('read'), false);
    return;
  }
  _personasPageLoaded = true;
  section.innerHTML = '<span class="empty-state">Loading...</span>';
  fetch('/api/personas').then(function(r) { return r.json(); }).then(function(data) {
    // 157: each persona's last edit comes from its own version history. A failed read shows "not recorded".
    return Promise.all(data.map(function(p) {
      var role = String(p.role || '');
      return fetch('/api/personas/' + encodeURIComponent(role) + '/history', {credentials: 'same-origin'})
        .then(function(r) { return r.ok ? r.json() : null; })
        .catch(function() { return null; })
        .then(function(history) { return {persona: p, history: history}; });
    }));
  }).then(function(rows) {
    var html = rows.map(function(row) {
      var role = String(row.persona.role || '');
      return personaPanel(role, 'read', '<div data-persona-role="' + esc(role) + '">' +
        '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:4px">' + agentLabel(role) + personaStageNote(role) + '</div>' +
        '<div class="muted">' + (row.persona.is_default ? 'Using the built-in default text.' : 'Custom text. Differs from the built-in default.') + '</div>' +
        '<div class="persona-markdown persona-read" tabindex="0">' + personaMarkdown(row.persona.instructions || '') + '</div>' +
        '<div style="margin-top:4px">' + personaLastEditedHtml(row.history) + '</div>' +
        '<div style="margin-top:4px"><a href="/config#personas/' + esc(encodeURIComponent(role)) + '" onclick="navigate(event,this.getAttribute(\'href\'))">Edit in Settings</a></div>' +
        '</div>');
    }).join('');
    section.innerHTML = html ? '<div class="persona-workspace" data-persona-context="read">'
      + personaSelector(rows.map(function(row) { return row.persona; }), 'read') + html + '</div>'
      : '<span class="empty-state">No personas configured</span>';
    var workspace = section.querySelector('[data-persona-context]');
    personaStatsMount(workspace);
    personaMount(workspace);
  }).catch(function() { section.innerHTML = '<span class="empty-state">Failed to load personas</span>'; });
}


// 125: one timer refreshes every elapsed readout from its own start point.
setInterval(function() { refreshElapsedReadouts(document); refreshRelativeTimes(document); refreshFreshness(); fitActivityToBoard(); }, 1000);

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
// 138: activity feed
// ---------------------------------------------------------------------------
var AF_MAX_ROWS = 2000;
var AF_SILENCE_MS = 40000;   // ~2.5 keepalive intervals (FR-026)
var _afGroupingActive = false; // older SSE payloads lack session attribution
var _afEntries = [];        // retained entries, oldest-first
var _afDropped = 0;         // 135: entries the server's bounded log has dropped, as of the newest seen
var _afCapacity = null;     // 135: the bound the server's log holds, sent with each entry
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
  _afDropped = 0;
  var feed = document.getElementById('activity-feed');
  if (feed) feed.innerHTML = '';
  afUpdateEmpty();
  afUpdateFilterOptions();
}

// 148: the card filter and the kind chips both have to let an entry through.
// 139: the symphony choice narrows the feed to the cards of that symphony.
function afInScope(e) {
  var s = typeof _lastState !== 'undefined' ? _lastState : null;
  return symphonyKeepsFeedEntry(s, currentSymphonyChoice(s), e);
}
function afMatches(e) { return (!_afFilter || e.card_id === _afFilter) && afVisible(e) && afInScope(e); }

function afAppend(entries) {
  var feed = document.getElementById('activity-feed');
  if (!feed || !entries || !entries.length) return;
  var html = '';
  var added = 0;
  var needsRedraw = false;
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    if (e == null) continue;
    // 135: a stream's marker is re-sent as its hidden count grows. Redraw it in place.
    if (_afSeen[e.seq]) { if (afRefreshMarker(e)) needsRedraw = true; continue; }
    if (e.session_id) _afGroupingActive = true;
    _afSeen[e.seq] = true;
    if (typeof e.log_dropped === 'number' && e.log_dropped > _afDropped) _afDropped = e.log_dropped;
    if (typeof e.log_capacity === 'number') _afCapacity = e.log_capacity;
    var prev = _afEntries[_afEntries.length - 1];
    e._afGroupKey = afCanGroup(prev, e) ? prev._afGroupKey : e.seq;
    // 135: a late arrival (a re-send whose creation the client never got) goes where its seq belongs.
    if (!afInsertEntry(e)) needsRedraw = true;
    // FR-032: any real entry ends the card's quiet episode.
    if (e.card_id && e.activity_type !== 'quiet') delete _afQuietCards[e.card_id];
    if (!afMatches(e)) continue;
    html = afRowHtml(e) + html;   // batch is oldest-first; newest ends up on top
    added++;
  }
  while (_afEntries.length > AF_MAX_ROWS) delete _afSeen[_afEntries.shift().seq];
  if (_afGroupingActive) {
    afSyncGroups(false);
    _afShownSig = afVisibleSig();
    afUpdateEmpty();
    afUpdateFilterOptions();
    return;
  }
  if (needsRedraw) {
    // Out of order, or a count changed in the ungrouped view: the rows are drawn again in order.
    afRender();
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
  _afShownSig = afVisibleSig();
  afUpdateEmpty();
  afUpdateFilterOptions();
}

// The keys of the rows the feed shows now, in order. afAppend and afRender keep _afShownSig
// equal to it, so a snapshot that changes nothing on screen does not re-render the feed.
var _afShownSig = '';
function afVisibleSig() {
  var keys = [];
  for (var i = 0; i < _afEntries.length; i++) {
    if (afMatches(_afEntries[i])) keys.push(String(_afEntries[i].seq));
  }
  return keys.join('|');
}

// 139: on every snapshot, re-render only if the rows on screen would change (a scope or a
// filter change). A full re-render re-announces every row, so it is not done for nothing.
function afSyncVisible() {
  if (afVisibleSig() !== _afShownSig) afRender();
}

function afRender() {
  // Full re-render: a filter or scope change, or a snapshot that changes the rows on screen.
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  _afShownSig = afVisibleSig();
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

// 135: a stream's marker arrives again with a larger hidden count. It keeps its place in the
// feed; only its count changes. Returns true when the ungrouped view must be drawn again.
function afRefreshMarker(e) {
  if (e.activity_type !== 'stream_truncated') return false;
  for (var i = _afEntries.length - 1; i >= 0; i--) {
    if (_afEntries[i].seq !== e.seq) continue;
    e._afGroupKey = _afEntries[i]._afGroupKey;
    _afEntries[i] = e;
    if (_afGroupingActive) { afSyncGroups(false); return false; }
    return true;
  }
  return false;
}

// 135: entries sit in seq order. Returns false when the entry was not the newest, so the caller
// draws the feed again rather than prepending a row in the wrong place.
function afInsertEntry(e) {
  var i = _afEntries.length;
  while (i > 0 && _afEntries[i - 1].seq > e.seq) i--;
  _afEntries.splice(i, 0, e);
  return i === _afEntries.length - 1;
}

function afUpdateEmpty() {
  var feed = document.getElementById('activity-feed');
  var empty = document.getElementById('af-empty');
  if (!feed || !empty) return;
  empty.style.display = feed.childElementCount ? 'none' : '';
  afUpdateDropped();
  // 157: the feed's cap follows the board whenever the rows change, not only on the next timer tick.
  if (typeof fitActivityToBoard === 'function') fitActivityToBoard();
}

// 135: the line above the feed for entries the bounded log has dropped. Hidden when none were.
function afUpdateDropped() {
  var note = document.getElementById('af-dropped');
  if (!note) return;
  var text = afDroppedText(_afDropped, _afCapacity, symphonyChoiceForNote());
  note.textContent = text;
  note.style.display = text ? '' : 'none';
}

function afUpdateFilterOptions() {
  var sel = document.getElementById('af-filter');
  if (!sel) return;
  // 139: a card filter set under another symphony does not survive the switch.
  if (_afFilter && !afInScope({card_id: _afFilter})) _afFilter = '';
  var seen = {}, cards = [];
  for (var i = 0; i < _afEntries.length; i++) {
    var e = _afEntries[i];
    if (!e.card_id || seen[e.card_id] || !afInScope(e)) continue;
    seen[e.card_id] = true;
    cards.push(e);
  }
  if (_afFilter && !seen[_afFilter]) cards.push({card_id:_afFilter, card_title:'(no retained activity)'});
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
    (_afHasMessage ? fmtTime(new Date(_afLastMsgAt).toISOString()) : 'none received yet');
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
  if (bnr && !live) {
    bnr.textContent = 'Reconnecting, Coordinare is restarting';
    bnr.style.display = 'block';
  }
}

function afNoteMessage() {
  _afLastMsgAt = Date.now();
  _afHasMessage = true;
  afSetLive(true);
  updateConnectionStatus('Live');
}

function afQuietTick() {
  // 139: the quiet markers and the tiles follow the selected symphony, like every other view.
  var s = currentScopedState();
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
var RETRY_INITIAL_MS = 1000;
var RETRY_MAX_MS = 30000;
var _sseRetryDelay = RETRY_INITIAL_MS;
function retryDelay(delay) {
  return Math.round(delay * (0.5 + Math.random() * 0.5));
}
function showRestarting() {
  if (banner) {
    banner.textContent = 'Reconnecting, Coordinare is restarting' +
      (currentScopedState() ? '' : '. Coordinare is starting');
    banner.style.display = 'block';
  }
  updateConnectionStatus('Reconnecting');
  var readiness = document.getElementById('daemon-readiness');
  if (readiness) readiness.textContent = ' · Daemon readiness unknown';
  var fpBtn = document.getElementById('force-poll-btn');
  if (fpBtn) fpBtn.disabled = true;
}
// Retry reads only. Replaying a mutation could execute an operator action twice.
if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
  var dashboardNativeFetch = window.fetch.bind(window);
  window.fetch = function(url, options) {
    if (typeof url !== 'string' || url.indexOf('/api/') !== 0) return dashboardNativeFetch(url, options);
    var method = ((options && options.method) || 'GET').toUpperCase();
    var read = method === 'GET' || method === 'HEAD';
    var delay = RETRY_INITIAL_MS;
    function retry() {
      return new Promise(function(resolve) {
        setTimeout(resolve, retryDelay(delay));
        delay = Math.min(delay * 2, RETRY_MAX_MS);
      }).then(attempt);
    }
    function attempt() {
      return dashboardNativeFetch(url, options).then(function(response) {
        if (response.status === 503) {
          showRestarting();
          if (read) return retry();
        }
        return response;
      }, function(error) {
        if (error.name === 'AbortError') throw error;
        showRestarting();
        if (read) return retry();
        throw error;
      });
    }
    return attempt();
  };
}
function connectEventStream() {
  var source = new EventSource('/events');
  es = source;
  source.addEventListener('activity_event', function(e) {
    if (es !== source) return;
    afNoteMessage();
    try {
      var entries = JSON.parse(e.data).entries;
      afAppend(entries);
      bootstrapLiveActivity(entries);
    }
    catch(err) { console.error('activity parse error', err); }
  });
  source.addEventListener('events_changed', function() { if (es === source) { afNoteMessage(); if (typeof logsChanged === 'function') logsChanged(); } });
  source.addEventListener('heartbeat', function() { if (es === source) { afNoteMessage(); if (typeof logsChanged === 'function') logsChanged(); } });
  source.addEventListener('state_update', function(e) {
    if (es !== source) return;
    afNoteMessage();
    _lastStateAt = Date.now();
    try {
      _lastState = JSON.parse(e.data);
      _sseRetryDelay = RETRY_INITIAL_MS;
      if (banner) banner.style.display = 'none';
      var readiness = document.getElementById('daemon-readiness');
      if (readiness) readiness.textContent = currentScopedState().daemon_ready === false ? ' · Daemon degraded' : ' · Daemon ready';
      // state_update precedes backfill on every stream connection.
      afSyncRun(_lastState.activity_run_id);
      // 139: the header control first, then every view narrowed to the symphony chosen there.
      renderSymphonySwitcher();
      var scoped = scopedView(_lastState);
      // 153: before the render, so the title and favicon follow the data even if renderState throws.
      updateAttention(scoped);
      renderState(scoped);
      // 049: re-render current page with new state
      var path = location.pathname;
      if (isPerformersPath(path)) renderPerformersPage(scoped);
      else if (path === '/personas') personaStatsRefresh(false);
      else if (path === '/history') renderHistoryPage(scoped);
      else if (path === '/logs') renderLogsPage(currentScopedState());
      else if (isBootstrapRunPath(path)) bootstrapPageRefresh();
      else if (path === '/symphonies' || path.startsWith('/symphonies/')) renderSymphoniesPage(_lastState);
      else if (isCardPath(path)) cardPageRefresh();
      else renderDashboardExtras(scoped);
      // 139: the feed follows the scope on every snapshot, so rows that arrived before it cannot linger.
      afUpdateFilterOptions();
      afSyncVisible();
    } catch(err) { console.error('parse error', err); }
  });
  source.onerror = function() {
    if (es !== source) return;
    showRestarting();
    // Own all retry timing, including network failures in CONNECTING state.
    source.close();
    if (_sseRetryTimer === null) {
      _sseRetryTimer = setTimeout(function() {
        _sseRetryTimer = null;
        if (es !== source) return;
        connectEventStream();
      }, retryDelay(_sseRetryDelay));
      _sseRetryDelay = Math.min(_sseRetryDelay * 2, RETRY_MAX_MS);
    }
  };
  source.onopen = function() {
    if (es !== source) return;
    if (typeof logsChanged === 'function') logsChanged();
    if (_sseRetryTimer !== null) {
      clearTimeout(_sseRetryTimer);
      _sseRetryTimer = null;
    }
    updateConnectionStatus('Waiting for updates');
  };
}
connectEventStream();

document.addEventListener('DOMContentLoaded', function() {
  afApplyChipState();   // 148: the chips show the choice this browser remembered
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

function symSummaryHtml(name, data) {
  var overrides = data.overrides || {};
  var rows = [['Enabled', data.enabled !== false ? 'Yes' : 'No']].concat(symOverrideFields().map(function(field) {
    var set = Object.prototype.hasOwnProperty.call(overrides, field.key);
    return [field.label, set ? (overrides[field.key] === null ? 'No cap' : String(overrides[field.key])) : 'Inherits global default'];
  }));
  rows.push(['Env spec files', (data.env_spec_files || ['README.md']).join(', ')]);
  var href = '/config#symphonies/' + encodeURIComponent(name);
  return '<section class="card" data-sym-summary-view><h3>Settings</h3><dl>' + rows.map(function(row) {
    return '<dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1]) + '</dd>';
  }).join('') + '</dl><p><a href="' + esc(href) + '" onclick="navigate(event,this.getAttribute(\'href\'))">Edit settings</a></p>'
    + '<div data-sym-persona-summary>Loading personas...</div></section>';
}

function loadSymphonyPersonas(name, data, el) {
  var target = el.querySelector('[data-sym-persona-summary]');
  return fetch('/api/personas').then(function(response) {
    if (!response.ok) throw new Error('Failed to load personas');
    return response.json();
  }).then(function(catalog) {
    if (!target.isConnected) return;
    target.innerHTML = symPersonasHtml(name, data.personas || {}, catalog, false);
    personaMount(target.querySelector('[data-persona-context]'));
  }).catch(function() {
    if (target.isConnected) target.textContent = 'Failed to load personas. Reload to try again.';
  });
}

function symValidateAndSave(name, body, pendingEdits, form, statusEl, btn) {
  var url = '/api/symphonies/' + encodeURIComponent(name);
  var version = _symCfgHash;
  cfgFlash(statusEl, 'Validating...', 'muted');
  var controller = typeof AbortController === 'function' ? new AbortController() : null;
  var timer = setTimeout(function() { if (controller) controller.abort(); }, CFG_SAVE_TIMEOUT_MS);
  return fetch(url + '/validate', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body), signal: controller ? controller.signal : undefined}).then(function(response) {
    return response.json().then(function(result) {
      clearTimeout(timer);
      if (!response.ok || !result.valid) {
        var errors = (result.errors || []).map(function(error) { return error.msg; }).join('; ');
        cfgFlash(statusEl, 'Not saved: ' + (result.error || errors || 'Validation failed') + '. Nothing was changed.', 'red');
        cfgLock(form, false);
        return;
      }
      _cfgPending = pendingEdits;
      return cfgPut('/api/symphonies/' + encodeURIComponent(name),
        {store: 'symphony', _method: 'PUT', 'If-Match': version || '', body: body}, form, statusEl, btn);
    });
  }).catch(function() {
    clearTimeout(timer);
    cfgFlash(statusEl, 'Validation could not finish. Nothing was saved. Try again.', 'red');
    cfgLock(form, false);
  });
}
