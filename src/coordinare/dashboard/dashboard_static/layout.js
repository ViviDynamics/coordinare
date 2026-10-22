// 154: the Dashboard's layout. The status strip, the active-run tiles and the
// utilization summary are composed here from the state the page already holds.
// The functions are pure (they read only their arguments and the shared helpers)
// except renderStatusStrip and showSystemHealth, which touch the strip and the
// health card. Every value goes through esc(). Loaded after systemhealth.js, before app.js.

// A stage run longer than this is marked stale on its tile and on its board card (moved from app.js, 154).
var STALE_THRESHOLD_MS = 30 * 60 * 1000;  // 30 minutes

// The status strip's counts: running cards, To Do cards queued for a slot, and
// the cards that need a person (the attention list, minus subsystem alarms, which
// the health dot already carries).
function statusCounts(s) {
  var needsHuman = attentionItems(s).filter(function(item) { return item.kind !== 'subsystem_down'; });
  return { active: activeRunSessions(s).length, queued: queuedTodoCount(s), needsHuman: needsHuman.length };
}

// The sessions with a stage running now: one active-run tile each.
function activeRunSessions(s) {
  var sessions = Array.isArray(s && s.active_sessions) ? s.active_sessions : [];
  return sessions.filter(isStageRunning);
}

// To Do cards that wait for a free slot. Counted with the same reason the To Do
// column's header reads (waiting.js), so the strip and the column cannot disagree.
function queuedTodoCount(s) {
  var sessions = Array.isArray(s && s.active_sessions) ? s.active_sessions : [];
  var sessionIds = {};
  sessions.forEach(function(sess) { if (sess && sess.card_id) sessionIds[sess.card_id] = true; });
  var todoWaits = {};
  var symphonies = Array.isArray(s && s.symphonies) ? s.symphonies : [];
  symphonies.forEach(function(sym) { Object.assign(todoWaits, (sym && sym.state && sym.state.todo_waits) || {}); });
  var snapshot = {
    session_skip_reasons: sessionSkipReasons(s),
    open_questions: (s && s.open_questions) || [],
    todo_waits: todoWaits,
    todo_capacity: null,
    now: Date.now(),
  };
  var count = 0;
  symphonies.forEach(function(sym) {
    var todo = sym && sym.state && sym.state.board_snapshot && Array.isArray(sym.state.board_snapshot.TODO)
      ? sym.state.board_snapshot.TODO : [];
    todo.forEach(function(cardId) {
      var reason = waitingReason({ id: cardId, column: 'TODO', has_session: !!sessionIds[cardId] }, snapshot);
      if (reason.kind === 'queued') count += 1;
    });
  });
  return count;
}

// "1 active · 0 queued · 0 need a person": the words the strip shows.
function statusCountsText(counts) {
  return counts.active + ' active · ' + counts.queued + ' queued · ' + counts.needsHuman + ' needs human';
}

// The overall health of a subsystem list, when the snapshot does not carry one (moved from app.js, 154).
function computeOverallHealth(subsystems) {
  var required = (subsystems || []).filter(function(s) { return s.required; });
  if (!required.length || required.every(function(s) { return s.status === 'healthy'; })) return 'healthy';
  if (required.some(function(s) { return s.status === 'unavailable'; })) return 'unavailable';
  return 'degraded';
}

// The health dot's tone and its label. The System health card carries the words.
function healthDotModel(s) {
  if (s && s.system_health) {
    var sum = systemHealthSummary(s.system_health);
    return { tone: sum.tone, label: 'System health: ' + sum.tone };
  }
  var overall = computeOverallHealth(s && s.subsystems);
  return { tone: overall === 'healthy' ? 'healthy' : (overall === 'unavailable' ? 'down' : 'degraded'), label: 'System health: ' + overall };
}

// The status strip, as data: the conductor's headline, the counts and the health dot.
function statusStripModel(s, activeSess) {
  return {
    headline: conductorHeadline(s, activeSess),
    counts: statusCounts(s),
    health: healthDotModel(s),
  };
}

// 266: the bootstrap states the strip shows: a bootstrap that is running, or one that is failing.
var STRIP_BOOTSTRAP_STATES = ['bootstrapping', 'verifying', 'failed', 'waiting_to_retry', 'budget_exhausted'];

// 266: the strip's bootstrap item. One symphony's bootstrap is named in words, with its reason when it failed.
// Several are counted, so the strip never names a symphony the page is not showing. Empty when none needs a word.
function bootstrapStripText(s) {
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  var live = syms.filter(function(sym) {
    var b = sym && sym.env_bootstrap;
    return !!b && typeof b === 'object' && STRIP_BOOTSTRAP_STATES.indexOf(b.state) !== -1;
  });
  if (!live.length) return '';
  if (live.length > 1) return 'Env bootstrap: ' + live.length + ' symphonies need attention';
  var b = live[0].env_bootstrap;
  var failing = b.state === 'failed' || b.state === 'waiting_to_retry' || b.state === 'budget_exhausted';
  return 'Env bootstrap: ' + String(b.label || '') + (failing && b.reason ? ' · ' + String(b.reason) : '');
}

// Writes the strip's dynamic parts. The strip's elements are static markup in the page.
function renderStatusStrip(s, activeSess) {
  var model = statusStripModel(s, activeSess);
  var phaseEl = document.getElementById('phase');
  if (phaseEl) phaseEl.textContent = model.headline;
  var countsEl = document.getElementById('status-counts');
  if (countsEl) countsEl.textContent = statusCountsText(model.counts);
  // 266: the bootstrap item sits after the counts. It is created once, and hidden when there is nothing to say.
  var bootEl = document.getElementById('status-bootstrap');
  if (!bootEl && countsEl && countsEl.parentNode) {
    bootEl = document.createElement('span');
    bootEl.id = 'status-bootstrap';
    bootEl.className = 'status-counts status-bootstrap';
    countsEl.parentNode.insertBefore(bootEl, countsEl.nextSibling);
  }
  if (bootEl) {
    var bootText = bootstrapStripText(s);
    bootEl.textContent = bootText;
    bootEl.title = bootText;
    bootEl.hidden = !bootText;
  }
  var dot = document.getElementById('health-dot');
  if (dot) {
    dot.className = 'status-health sh-tone-' + esc(model.health.tone);
    dot.setAttribute('aria-label', model.health.label);
    dot.title = model.health.label;
  }
}

// The Cards header's counts: the total of the four board columns, and the Done and
// The column counts the Cards header states. They come from the same symphony boards the columns are
// drawn from, so the header and the columns cannot disagree. The top-level board summary is read only
// when no symphony has a board yet. Total is every card the board shows: the four columns and Done.
function boardColumnCounts(s) {
  var boards = (s && Array.isArray(s.symphonies) ? s.symphonies : []).filter(function(sym) {
    var snap = sym && sym.state && sym.state.board_snapshot;
    return !!snap && typeof snap === 'object';
  });
  var counts = {TODO: 0, BLOCKED: 0, IN_PROGRESS: 0, IN_REVIEW: 0, DONE: 0, BACKLOG: 0};
  if (boards.length === 0) {
    var summary = (s && s.board_summary && typeof s.board_summary === 'object') ? s.board_summary : {};
    Object.keys(counts).forEach(function(key) { counts[key] = Number.isFinite(summary[key]) ? summary[key] : 0; });
    return counts;
  }
  boards.forEach(function(sym) {
    var snap = sym.state.board_snapshot;
    Object.keys(counts).forEach(function(key) {
      if (Array.isArray(snap[key])) counts[key] += snap[key].length;
    });
  });
  return counts;
}

function boardCountsText(s) {
  var counts = boardColumnCounts(s);
  var total = counts.TODO + counts.BLOCKED + counts.IN_PROGRESS + counts.IN_REVIEW + counts.DONE;
  return 'Total ' + total + ' · Done ' + counts.DONE + ' · Backlog ' + counts.BACKLOG;
}

// The cost on an active run's tile, in one vocabulary. The daemon always reports the ledger's state on a
// session: tracked false when the usage ledger is off ("not tracked"). When it is on, a run with nothing
// recorded yet is "no usage yet". Otherwise the cost, or "unknown" with its reason on hover.
function activeRunCostHtml(sess) {
  var usage = sess && sess.usage;
  if (!usage || usage.tracked === false) return usageCostHtml(usage || null);
  var summary = usageSummaryOf(sess, 'card');
  if (!summary || !summary.tokens || !(Number(summary.tokens.total) > 0)) {
    return '<span title="' + esc(USAGE_PENDING_NOTE) + '">no usage yet</span>';
  }
  return usageCostHtml(summary);
}

// One summary line for the active-runs header: "8 agents · 1 busy · 0 queued".
// Empty when the daemon reports no performer roles.
function utilizationSummaryText(s) {
  var roles = Array.isArray(s && s.role_utilization) ? s.role_utilization : [];
  if (roles.length === 0) return '';
  var busy = 0;
  var queued = 0;
  roles.forEach(function(r) {
    busy += Number(r && r.active) || 0;
    queued += Number(r && r.queued) || 0;
  });
  return roles.length + (roles.length === 1 ? ' agent' : ' agents') + ' · ' + busy + ' busy · ' + queued + ' queued';
}

// The tokens figure on an active run's tile: the card's usage (134), or a pending
// marker while the stage runs and nothing is reported yet (122).
function activeRunTokens(sess) {
  var cardUsage = usageSummaryOf(sess, 'card');
  var fallback = sess.card_tokens_total || derivePerformerTokenTotal(sess);
  return tokenReadout(usageTotalOf(cardUsage, fallback), true);
}

// The idle state of the active-runs section: no run, and the filter hint when one is set.
function activeRunsIdleHtml(s) {
  var filterHint = s && s.ownership_hint ? esc(s.ownership_hint) : '';
  var board = s && s.board_summary;
  var emptyBoard = board && Object.keys(board).length > 0 &&
    Object.keys(board).every(function(status) { return Number(board[status]) === 0; });
  return '<div class="ap-idle">' +
    '<div class="ap-idle-title">No active performers</div>' +
    (s && s.idle_reason ? '<p>' + esc(s.idle_reason) + '</p>' : '') +
    (filterHint ? '<div class="ap-idle-rows"><div class="ap-idle-row">' +
      '<span class="ap-pill">Filter: <strong>' + filterHint + '</strong></span></div></div>' : '') +
    (emptyBoard ? '<details><summary>Try a first card</summary>' +
    '<p>Create an issue titled <strong>Document our first Coordinare run</strong>.</p>' +
    '<p>Create <code>FIRST_CARD.md</code> at the repository root with one sentence: ' +
    'Coordinare can open a pull request for this repository. Change no other files. ' +
    'Open a pull request for a human to review.</p>' +
    '<p>Add the issue to your board and set Status to To Do or Ready. ' +
    'Assign it according to the filter above. Watch it move to In Progress, then In Review.</p>' +
    '</details>' : '') + '</div>';
}

// One running card: its stage and phase, the title, the pipeline tracker, the
// elapsed time, tokens and cost, the run, and the card's action menu.
function activeRunTileHtml(sess, s) {
  var id = esc(String(sess.card_id || ''));
  var stage = sess.performer_stage ? stageAgentLabel(sess.performer_stage) : '-';
  var title = sess.card_title || sess.card_id || '-';
  var dispatchAt = sess.agent_dispatch_at || s.agent_dispatch_at;
  var phaseLabel = sess.phase === 'blocked'
    ? esc(blockedSessionText(sess, s.open_questions, sessionSkipReasons(s)))
    : formatPhaseLabel(sess.phase || 'active');
  var tokens = activeRunTokens(sess);
  var costCell = activeRunCostHtml(sess);
  // The stale marker the board card carried for a long run (performers.js, 59 and after).
  var stale = dispatchAt && (Date.now() - new Date(dispatchAt).getTime()) > STALE_THRESHOLD_MS;
  var staleGlyph = stale ? '<span style="color:var(--color-degraded)">&#9888;</span> ' : '';
  // 154 review: four short rows so a tile is about 120px tall and four sit side by side at 1280.
  // Row 1: the stage and the card's menu. Row 2: the title. Row 3: the stage tracker and the phase.
  // Row 4: the facts, as one line of text.
  return '<div class="ap-tile" data-card-id="' + id + '">' +
    '<div class="ap-tile-header">' +
      '<div class="ap-tile-role"><span class="perf-dot perf-running" aria-hidden="true"></span>' + stage + '</div>' +
      cardActionsHtml(sess, s) +
    '</div>' +
    '<div class="ap-tile-title" title="' + esc(title) + '">' + esc(title) + ' ' + cardRefHtml(sess) + '</div>' +
    '<div class="ap-tile-track">' +
      timelineDotsHtml(cardPipelineOf(s, sess.card_id)) +
      '<span class="ap-tile-phase">' + phaseLabel + '</span>' +
    '</div>' +
    '<div class="ap-tile-meta">' +
      '<span class="ap-fact ap-tile-elapsed">' + staleGlyph + '&#9201; ' + elapsedSinceHtml(dispatchAt) + '</span>' +
      '<span class="ap-fact">Tokens <strong class="ap-tile-tokens" title="' + esc(tokens.title) + '">' + esc(tokens.text) + '</strong></span>' +
      '<span class="ap-fact">Cost <span class="ap-tile-cost">' + costCell + '</span></span>' +
      (sess.session_id ? '<span class="ap-fact">' + runIdHtml(sess.session_id, {copy: true}) + '</span>' : '') +
      viewRunLinkHtml(sess) +
      pendingControlHtml(sess) +
      // 138 FR-032: quiet marker, observation only, never a phase change.
      (_afQuietCards[sess.card_id] ? '<span class="af-kind ev-quiet">QUIET</span>' : '') +
    '</div>' +
  '</div>';
}

// 154 review 2: a board column that scrolls says so. A column whose cards run past its
// height gets the fade (is-overflowing), a "+N more" line for the cards whose tops are
// out of view, and a tab stop so a keyboard user can scroll it. Runs after each board render.
function markBoardOverflow() {
  var columns = document.querySelectorAll('#swimlane-section .swimlane-col');
  for (var i = 0; i < columns.length; i++) {
    var col = columns[i];
    var body = col.querySelector('.swimlane-col-body');
    // Nothing to measure while the board is hidden (the card detail is open, or another page shows).
    if (!body || body.clientHeight === 0) continue;
    var cards = Array.prototype.filter.call(body.children, function(c) { return c.classList.contains('swimlane-card'); });
    snapBoardColumn(body, cards);
    var height = body.clientHeight;
    var floor = body.scrollTop + height;
    // A card cut by the foot of the column is still more content below, so it counts too.
    var below = cards.filter(function(c) { return c.offsetTop + c.offsetHeight > floor; }).length;
    var overflowing = body.scrollHeight > height + 1;
    col.classList.toggle('is-overflowing', overflowing);
    if (overflowing) body.setAttribute('tabindex', '0'); else body.removeAttribute('tabindex');
    var more = col.querySelector('.swimlane-more');
    if (!more) {
      more = document.createElement('div');
      more.className = 'swimlane-more';
      col.appendChild(more);
    }
    more.textContent = '+' + below + ' more';
    more.hidden = below === 0;
    if (!body.getAttribute('data-overflow-wired')) {
      body.setAttribute('data-overflow-wired', '1');
      body.addEventListener('scroll', markBoardOverflow);
    }
  }
}

// 358: a column that scrolls ends on a whole card, so the fade and the "+N more" line sit
// under the last card shown rather than across one cut in half. The stylesheet's cap is read
// once per body; the snapped height depends only on the cards, so scrolling never moves it.
// A pixel of slack matches the overflow test below, which also allows one.
function snapBoardColumn(body, cards) {
  if (!body.getAttribute('data-cap-px')) {
    body.style.maxHeight = '';
    body.setAttribute('data-cap-px', String(parseFloat(getComputedStyle(body).maxHeight) || 0));
  }
  var cap = parseFloat(body.getAttribute('data-cap-px'));
  if (!(cap > 0)) return;
  var padBottom = parseFloat(getComputedStyle(body).paddingBottom) || 0;
  // Bounding rects, not offsetTop: offsets round to whole pixels and a fraction is enough to show a sliver.
  var origin = body.getBoundingClientRect().top + body.clientTop - body.scrollTop;
  var bottomOf = function(card) { return card.getBoundingClientRect().bottom - origin; };
  var fit = 0;
  for (var i = 0; i < cards.length; i++) {
    var bottom = bottomOf(cards[i]);
    if (bottom + padBottom > cap + 1) break;
    fit = bottom;
  }
  var overflows = cards.length > 0 && bottomOf(cards[cards.length - 1]) + padBottom > cap + 1;
  body.style.maxHeight = overflows && fit > 0 ? (fit + padBottom) + 'px' : '';
}

// The health dot: takes the reader to the System health card and moves focus there.
function showSystemHealth() {
  var card = document.getElementById('health-card');
  if (!card) return;
  card.setAttribute('tabindex', '-1');
  card.scrollIntoView({ block: 'nearest' });
  card.focus();
}

// 157: on a desktop the activity feed is as tall as its entries, but never taller than the board beside
// it. The log's cap is the board's height less the card's own chrome (its heading and controls). Below
// the desktop width the two stack, so the cap is removed and the stylesheet default applies.
// 157: the feed never collapses below this many pixels, however short the board beside it is.
var ACTIVITY_MIN_LOG_PX = 120;
function fitActivityToBoard() {
  var board = document.getElementById('active-work-card');
  var card = document.getElementById('activity-feed-card');
  var log = card ? card.querySelector('.af-log') : null;
  if (!board || !card || !log) return;
  var desktop = typeof window.matchMedia === 'function' && window.matchMedia('(min-width: 1100px)').matches;
  if (!desktop) { log.style.maxHeight = ''; return; }
  // A hidden board (the card detail is open) measures as 0; keep the last cap rather than collapse the feed.
  if (board.offsetHeight === 0) return;
  log.style.maxHeight = '';
  var chrome = card.offsetHeight - log.offsetHeight;
  log.style.maxHeight = Math.max(ACTIVITY_MIN_LOG_PX, board.offsetHeight - chrome) + 'px';
}
