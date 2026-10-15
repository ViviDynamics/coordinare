// 139: the symphony switcher. One choice, All symphonies or one symphony, filters
// every panel that shows cards, performers, activity, attention items, waiting
// reasons and spend. The choice comes from a ?symphony=<name> deep link, then
// from this browser's last choice, then All.
//
// The functions above the DOM section are pure: they take a snapshot and return
// data, so tests/js/symphony_switcher_checks.js can run them under node.

var SYMPHONY_ALL = '';                       // the choice that shows every symphony
var SYMPHONY_PARAM = 'symphony';             // the deep-link query parameter
var SYMPHONY_STORAGE_KEY = 'coordinare.symphony';
var SYMPHONY_BOARD_COLUMNS = ['TODO', 'IN_PROGRESS', 'IN_REVIEW', 'DONE', 'BLOCKED', 'BACKLOG'];

function symphonyHas(obj, key) {
  return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
}

// The symphony names in the snapshot, in order, without blanks or repeats.
function symphonyNames(s) {
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  var names = [];
  syms.forEach(function(sym) {
    var name = sym && typeof sym.name === 'string' ? sym.name : '';
    if (name && names.indexOf(name) === -1) names.push(name);
  });
  return names;
}

// The snapshot entry for one symphony name, or a bare entry when it is unknown.
function symphonyEntry(s, name) {
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  for (var i = 0; i < syms.length; i++) {
    if (syms[i] && syms[i].name === name) return syms[i];
  }
  return {name: name};
}

// The choice in force. A name the snapshot does not know is never used, so a
// stale link or a removed symphony falls back to All instead of hiding everything.
function resolveSymphonyChoice(fromUrl, stored, names) {
  var known = names || [];
  if (fromUrl && known.indexOf(fromUrl) !== -1) return fromUrl;
  if (stored && known.indexOf(stored) !== -1) return stored;
  return SYMPHONY_ALL;
}

// The value of ?symphony= in a query string, or '' when it is absent or malformed.
function symphonyParamFromSearch(search) {
  var query = String(search || '').replace(/^\?/, '');
  var pairs = query.split('&');
  for (var i = 0; i < pairs.length; i++) {
    var pair = pairs[i].split('=');
    if (pair[0] !== SYMPHONY_PARAM || pair.length < 2) continue;
    try { return decodeURIComponent(pair[1]); } catch (e) { return ''; }
  }
  return '';
}

// The symphony whose board snapshot, titles or live sessions hold this card.
// '' when no symphony in the snapshot knows the card.
function symphonyOfCard(s, cardId) {
  var id = String(cardId || '');
  if (!id) return '';
  var syms = s && Array.isArray(s.symphonies) ? s.symphonies : [];
  for (var i = 0; i < syms.length; i++) {
    var sym = syms[i] || {};
    if (sym.name && symphonyHoldsCard(sym.state, id)) return String(sym.name);
  }
  return '';
}

function symphonyHoldsCard(state, id) {
  if (!state || typeof state !== 'object') return false;
  var snap = state.board_snapshot;
  if (snap && typeof snap === 'object') {
    for (var col in snap) {
      if (symphonyHas(snap, col) && Array.isArray(snap[col]) && snap[col].indexOf(id) !== -1) return true;
    }
  }
  return symphonyHas(state.board_titles, id) || symphonyHas(state.active_sessions, id);
}

// Whether an item that names a card belongs under the choice. An item that names
// no card is about the whole conductor (a required subsystem, a daemon-level
// event), and an item on a card no symphony claims could be anyone's, so both
// stay under every choice. Only another symphony's card is hidden.
function symphonyKeepsCard(s, choice, cardId) {
  if (!choice || !cardId) return true;
  var owner = symphonyOfCard(s, cardId);
  return owner === '' || owner === choice;
}

// Counts per board column, summed over the symphonies given.
function symphonyBoardSummary(syms) {
  var out = {};
  SYMPHONY_BOARD_COLUMNS.forEach(function(col) {
    out[col] = 0;
    (syms || []).forEach(function(sym) {
      var snap = sym && sym.state && sym.state.board_snapshot;
      if (snap && Array.isArray(snap[col])) out[col] += snap[col].length;
    });
  });
  return out;
}

// One symphony's spend, in the shape renderSpend reads. A symphony with no
// figures of its own says so, rather than showing the deployment's total.
function symphonySpend(block, choice) {
  if (!block || !block.tracked) return block;
  var per = symphonyHas(block.by_symphony, choice) ? block.by_symphony[choice] : null;
  if (!per) {
    return {tracked: false, reason: 'No spend is recorded for this symphony.', day: block.day, week_start: block.week_start, today: null, week: null};
  }
  return {tracked: true, reason: null, day: block.day, week_start: block.week_start, today: per.today, week: per.week, by_symphony: {}};
}

// The snapshot as one symphony sees it. All symphonies, or a deployment with a
// single symphony, returns the snapshot unchanged.
// Every top-level snapshot field, and how a selected symphony scopes it. A field
// the server adds must get a row here: tests/js/snapshot_scope_checks.js and
// tests/unit/test_139_review_fixes.py fail until it does.
//   scoped  narrowed to the symphony's own rows (its sessions, questions, counts, spend)
//   derived rebuilt from the symphony's own entry in symphonies[] or its own session
//   shared  deployment-wide; shown under every symphony, labelled as covering all of them
//   hidden  not known per symphony; cleared under a symphony and named in scope_hidden
var SNAPSHOT_FIELD_SCOPE = {
  activity_quiet_threshold_seconds: 'shared',
  stall_timeout_seconds: 'shared',
  phase: 'derived',
  phase_label: 'derived',
  active_card_title: 'derived',
  active_card_column: 'derived',
  active_card_issue_url: 'derived',
  pr_url: 'derived',
  agent_session_id: 'derived',
  agent_dispatch_at: 'derived',
  open_questions: 'scoped',
  card_clarifications: 'derived',
  performer_events: 'derived',
  performer_metrics: 'derived',
  performer_stage: 'derived',
  lifecycle_sequence: 'derived',
  performer_backend: 'derived',
  performer_logs: 'derived',
  card_tokens_total: 'derived',
  card_cost_estimate: 'derived',
  active_session_count: 'derived',
  active_sessions: 'scoped',
  subsystems: 'shared',
  overall_health: 'shared',
  // 151, 266: the System health card's rows. The env reason is written for the symphony the page shows.
  system_health: 'derived',
  project_name: 'shared',
  project_board_url: 'shared',
  cycles_completed: 'derived',
  last_cycle_duration_seconds: 'hidden',
  consecutive_error_count: 'derived',
  // Branches that need a person stay under every scope, attributed or labelled as covering all
  // symphonies; the healthy ones are hidden under a symphony.
  daemon_start_time: 'shared',
  poll_interval_seconds: 'derived',
  token_usage: 'scoped',
  // 147: each card's board strip, keyed by symphony; a symphony sees only its own cards' strips.
  card_pipeline: 'scoped',
  activity_run_id: 'shared',
  cycle_history: 'hidden',
  cycle_active: 'shared',
  daemon_running: 'shared',
  blocked_by_dependencies: 'hidden',
  last_rebase_round: 'derived',
  role_utilization: 'shared',
  assignee_filter: 'shared',
  include_unassigned: 'shared',
  ownership_hint: 'derived',
  idle_reason: 'derived',
  board_summary: 'scoped',
  last_poll_at: 'derived',
  backend_ui_url: 'hidden',
  session_stats: 'hidden',
  session_skip_reasons: 'derived',
  symphonies: 'scoped',
  // 155: the server's clock when the snapshot was taken; the Done lane's 24-hour cut reads it.
  snapshot_at: 'shared',
  coordinare: 'shared'
};

// The fields of one session row (active_sessions[], built by _session_summary and
// _top_state_summary). A row is filtered with its session, so these are declared once,
// here, rather than per scope. tests/js/snapshot_scope_checks.js fails on a row key that
// is missing from this list.
var SESSION_ROW_FIELDS = [
  'agent_dispatch_at', 'backend_ui_url', 'block_cause', 'card_cost_estimate', 'card_id', 'card_title', 'cancel_available',
  'card_tokens_total', 'container_id', 'feedback_cycle_count', 'full_log_available', 'issue_number',
  'issue_url', 'observer_verdicts', 'pending_control', 'performer_backend', 'performer_events',
  'performer_logs', 'performer_metrics', 'performer_stage', 'phase', 'pr_url', 'session_id',
  'session_stats', 'slot_queued_since', 'total_feedback_cycles', 'triage_blocks', 'usage',
  'workflow_step', 'workflow_step_entered_at', 'workflow_step_trail'
];

// Every read of the full snapshot, _lastState, in the shipped scripts. A read goes through
// scopedView() or currentScopedState(); the reads listed here must see every symphony, and
// each says why. tests/js/last_state_scope_checks.js fails on an unlisted read and on an
// entry that no longer matches a line.
var SCOPE_UNSCOPED_READS = [
  {file: 'app.js', text: 'var _lastState = null;', why: 'the one holder of the full snapshot'},
  {file: 'app.js', text: '_lastState = JSON.parse(e.data);', why: 'the arrival of each snapshot, from which every scope is built'},
  {file: 'app.js', text: 'afSyncRun(_lastState.activity_run_id);', why: 'the feed run belongs to the daemon, not to a symphony'},
  {file: 'app.js', text: 'renderSymphoniesPage(_lastState)', why: 'the Symphonies page lists every symphony by design'},
  {file: 'app.js', text: "var s = typeof _lastState !== 'undefined' ? _lastState : null;", why: 'the feed must know every symphony to tell whose card an item is'},
  {file: 'symphonies.js', text: "var s = typeof _lastState !== 'undefined' ? _lastState : null;", why: 'the switcher lists every symphony to offer its choices'},
  {file: 'symphonies.js', text: "symphonyNames(typeof _lastState !== 'undefined' ? _lastState : null)", why: 'the switcher resolves a choice against every symphony'},
  {file: 'symphonies.js', text: "if (typeof _lastState === 'undefined' || !_lastState) return;", why: 'the one place a scoped refresh starts'},
  {file: 'symphonies.js', text: "if (target.indexOf('?') !== -1 || typeof _lastState === 'undefined' || !_lastState) return target + hash;", why: 'a link carries the choice, which needs every symphony'},
  {file: 'symphonies.js', text: 'if (symphonyNames(_lastState).length < 2) return target + hash;', why: 'a link carries the choice, which needs every symphony'},
  {file: 'symphonies.js', text: 'var choice = currentSymphonyChoice(_lastState);', why: 'a link carries the choice, which needs every symphony'}
];

// The phases that make a session the one the conductor is watching.
var SYMPHONY_FOCUS_PHASES = ['monitoring_performer', 'monitoring_agent', 'relay_feedback'];

// A copy of an item on a card no symphony claims, marked so the page can say so.
function symphonyUnclaimed(item) {
  var copy = {};
  for (var key in item) {
    if (symphonyHas(item, key)) copy[key] = item[key];
  }
  copy.unclaimed = true;
  return copy;
}

// The session the conductor's own fields describe under a symphony: the first of
// its own sessions in a monitoring phase, else its first session, else none.
function symphonyFocusSession(own) {
  for (var i = 0; i < own.length; i++) {
    if (SYMPHONY_FOCUS_PHASES.indexOf(own[i].phase) !== -1) return own[i];
  }
  return own.length > 0 ? own[0] : null;
}

// The snapshot as one symphony sees it. All symphonies, or a deployment with a
// single symphony, returns the snapshot unchanged. Items on a card no symphony
// claims stay, marked unclaimed, so nothing that needs a person is hidden.
function symphonyScope(s, choice) {
  if (!s || !choice || symphonyNames(s).length < 2) return s;
  var mine = symphonyEntry(s, choice);
  var state = mine.state || {};
  var scoped = {};
  for (var key in s) {
    if (symphonyHas(s, key)) scoped[key] = s[key];
  }

  // Sessions: the symphony's own, and the unclaimed ones marked as such.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var kept = [];
  var own = [];
  sessions.forEach(function(sess) {
    if (!sess) return;
    var owner = symphonyOfCard(s, sess.card_id);
    if (owner === choice) { kept.push(sess); own.push(sess); }
    else if (owner === '') { kept.push(symphonyUnclaimed(sess)); }
  });
  scoped.active_sessions = kept;
  scoped.active_session_count = own.length;

  // Questions: the symphony's own, and the unclaimed ones marked as such.
  var questions = Array.isArray(s.open_questions) ? s.open_questions : [];
  scoped.open_questions = questions.reduce(function(out, q) {
    if (!q || typeof q !== 'object') { out.push(q); return out; }
    var owner = symphonyOfCard(s, q.card_id);
    if (owner === choice) out.push(q);
    else if (owner === '') out.push(symphonyUnclaimed(q));
    return out;
  }, []);

  // The conductor's own fields: from the focus session, or idle when it has none.
  var focus = symphonyFocusSession(own);
  scoped.phase = focus ? focus.phase : 'idle';
  scoped.phase_label = null;
  scoped.active_card_title = focus ? focus.card_title || null : null;
  scoped.active_card_column = null;
  scoped.active_card_issue_url = focus ? focus.issue_url || null : null;
  scoped.pr_url = focus ? focus.pr_url || null : null;
  scoped.agent_session_id = focus ? focus.session_id || null : null;
  scoped.agent_dispatch_at = focus ? focus.agent_dispatch_at || null : null;
  scoped.card_clarifications = [];
  scoped.performer_events = focus && Array.isArray(focus.performer_events) ? focus.performer_events : [];
  scoped.performer_metrics = focus ? focus.performer_metrics || null : null;
  scoped.performer_stage = focus ? focus.performer_stage || '' : '';
  scoped.lifecycle_sequence = [];
  scoped.performer_backend = focus ? focus.performer_backend || '' : '';
  scoped.performer_logs = focus && Array.isArray(focus.performer_logs) ? focus.performer_logs : [];
  scoped.card_tokens_total = focus ? focus.card_tokens_total || 0 : 0;
  scoped.card_cost_estimate = focus ? focus.card_cost_estimate || 0 : 0;

  // The symphony's own counts, poll timing and settings.
  scoped.cycles_completed = symphonyNumber(mine.cycle_count);
  scoped.consecutive_error_count = symphonyNumber(mine.error_count);
  scoped.poll_interval_seconds = symphonyNumber(mine.poll_interval_seconds);
  scoped.last_poll_at = mine.last_poll_at || null;
  scoped.ownership_hint = typeof mine.ownership_hint === 'string' ? mine.ownership_hint : '';
  scoped.idle_reason = typeof mine.idle_reason === 'string' ? mine.idle_reason : '';
  scoped.session_skip_reasons = state.session_skip_reasons || {};

  // The symphony list, counts and spend, narrowed to the choice.
  scoped.symphonies = (Array.isArray(s.symphonies) ? s.symphonies : []).filter(function(sym) {
    return !!sym && sym.name === choice;
  });
  scoped.board_summary = symphonyBoardSummary(scoped.symphonies);
  scoped.token_usage = symphonySpend(s.token_usage, choice);
  // 266: the health row's env reason is written for this symphony (symphonyHealthScope).
  scoped.system_health = symphonyHealthScope(s.system_health, choice);
  // 147: the board strips of this symphony's cards, and no other symphony's.
  var strips = s.card_pipeline && typeof s.card_pipeline === 'object' ? s.card_pipeline : {};
  scoped.card_pipeline = {};
  if (symphonyHas(strips, choice)) scoped.card_pipeline[choice] = strips[choice];

  // Hidden: values the snapshot does not carry per symphony. Each is cleared and named.
  scoped.scope_hidden = ['backend_ui_url', 'blocked_by_dependencies', 'cycle_history',
    'last_cycle_duration_seconds', 'session_stats'];
  scoped.backend_ui_url = null;
  scoped.blocked_by_dependencies = [];
  scoped.cycle_history = [];
  scoped.last_cycle_duration_seconds = null;
  scoped.last_rebase_round = symphonyRebaseAttention(s, s.last_rebase_round);
  scoped.session_stats = null;
  return scoped;
}

// 266: the System health row's env reason for the symphony the page shows. The failure is named only for
// that symphony; the other failing symphonies are counted. All symphonies (choice '') keeps every reason.
// A row without env failures, or a health object without rows, is returned unchanged.
function symphonyHealthScope(health, choice) {
  if (!health || typeof health !== 'object' || !Array.isArray(health.rows) || !choice) return health;
  var rows = health.rows.map(function(row) {
    if (!row || row.key !== 'disk_env_cache' || !Array.isArray(row.env_failures) || row.env_failures.length === 0) return row;
    var mine = row.env_failures.filter(function(f) { return !!f && f.symphony === choice; });
    var others = row.env_failures.length - mine.length;
    var parts = mine.map(function(f) { return String(f.reason || 'the reason was not recorded'); });
    if (others > 0) parts.push(others === 1 ? '1 other symphony has a failing bootstrap' : others + ' other symphonies have failing bootstraps');
    var copy = {};
    for (var key in row) {
      if (symphonyHas(row, key)) copy[key] = row[key];
    }
    if (parts.length) copy.reason = 'env cache bootstrap failed: ' + parts.join('; ');
    copy.env_failures = mine;
    return copy;
  });
  var out = {};
  for (var field in health) {
    if (symphonyHas(health, field)) out[field] = health[field];
  }
  out.rows = rows;
  return out;
}

// 266: whether one feed entry shows under the choice. A bootstrap row carries its symphony and shows only under
// that symphony, or under All. Any other entry is kept by its card, as symphonyKeepsCard decides.
function symphonyKeepsFeedEntry(s, choice, entry) {
  if (entry && entry.symphony) return !choice || choice === SYMPHONY_ALL || entry.symphony === choice;
  return symphonyKeepsCard(s, choice, entry ? entry.card_id : '');
}

// The branch-divergence round under a symphony. A blocked or failed branch needs a person, so
// it stays under every scope: attributed to its symphony when its card is known, and marked
// unattributed when no symphony claims it. A healthy branch is informational, so it is dropped
// and the round says some were (partial). Null when nothing needs a person.
function symphonyRebaseAttention(s, round) {
  if (!round || typeof round !== 'object' || !Array.isArray(round.jobs)) return round || null;
  var needing = [];
  var healthy = 0;
  round.jobs.forEach(function(job) {
    if (!job || typeof job !== 'object') return;
    if (job.outcome !== 'blocked' && job.outcome !== 'failed') { healthy += 1; return; }
    var owner = symphonyOfCard(s, job.card_id);
    var copy = {};
    for (var key in job) {
      if (symphonyHas(job, key)) copy[key] = job[key];
    }
    copy.symphony = owner;
    copy.unattributed = owner === '';
    needing.push(copy);
  });
  if (needing.length === 0) return null;
  var out = {};
  for (var field in round) {
    if (symphonyHas(round, field)) out[field] = round[field];
  }
  out.jobs = needing;
  out.partial = healthy > 0;
  return out;
}

// The full snapshot as the views read it: the snapshot narrowed to the symphony the header
// chose. Every render takes this, so no view can show another symphony's data by reading the
// unscoped snapshot. Null before the first snapshot arrives.
function currentScopedState() {
  return typeof _lastState !== 'undefined' && _lastState ? scopedView(_lastState) : null;
}

// A count from a symphony entry, or null when the snapshot does not carry it.
function symphonyNumber(value) {
  return typeof value === 'number' && isFinite(value) ? value : null;
}

// The notes under the panels: what is known for every symphony, and what is not known per symphony.
var SCOPE_NOTE_SHARED = 'Covers all symphonies.';
var SCOPE_NOTE_CAPACITY = 'Capacity and busy counts are shared by all symphonies.';
var SCOPE_NOTE_CHECKS = 'Recent checks are shown for all symphonies only.';
var SCOPE_NOTE_DEPENDENCIES = 'Dependency blockers are shown for all symphonies only.';
var SCOPE_NOTE_HEALTHY_BRANCHES = 'Healthy branches are shown for all symphonies only.';
var SCOPE_NOTE_DURATION = 'Shown for all symphonies only.';

// Writes the notes for one snapshot: the dashboard panels and the History page share the
// recent-checks note, so a panel the scope hides says the same thing wherever it is.
function applyScopeNotes(s) {
  var scoped = !!(s && Array.isArray(s.scope_hidden));
  var hidden = scoped ? s.scope_hidden : [];
  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  setText('health-scope-note', scoped ? SCOPE_NOTE_SHARED : '');
  setText('capacity-scope-note', scoped ? SCOPE_NOTE_CAPACITY : '');
  var last = document.getElementById('last-duration');
  if (last) last.title = hidden.indexOf('last_cycle_duration_seconds') !== -1 ? SCOPE_NOTE_DURATION : '';
  // 150/154: the History page's runs come from the server under the same scope. Only its
  // Recent checks panel (the daemon's cycle log) carries this note.
  if (hidden.indexOf('cycle_history') !== -1) {
    var recent = document.getElementById('history-cycles');
    if (recent) recent.innerHTML = '<span class="empty-state">' + SCOPE_NOTE_CHECKS + '</span>';
  }
  var depList = document.getElementById('dependency-blockers-list');
  var depCard = document.getElementById('dependency-blockers-card');
  if (depList && depCard && hidden.indexOf('blocked_by_dependencies') !== -1) {
    depCard.style.display = '';
    depList.innerHTML = '<li class="empty-state">' + SCOPE_NOTE_DEPENDENCIES + '</li>';
  }
  var rebaseList = document.getElementById('rebase-status-list');
  var rebaseCard = document.getElementById('rebase-status-card');
  var round = s && s.last_rebase_round;
  if (rebaseList && rebaseCard && round && round.partial && round.jobs && round.jobs.length > 0) {
    rebaseList.insertAdjacentHTML('beforeend', '<li class="empty-state">' + SCOPE_NOTE_HEALTHY_BRANCHES + '</li>');
  }
}

// Only an https link is ever written into the page.
function safeHttpUrl(url) {
  var text = String(url || '');
  return /^https?:\/\//i.test(text) ? text : '';
}

// 155: a symphony that has never been polled and holds no cards. Its config has not been
// checked, so the switcher and the Symphonies page say so. It is greyed out, not hidden,
// because hiding it would also hide the config problem that keeps it empty.
var SYMPHONY_NEVER_CHECKED_TEXT = 'Never checked: check config';

// The cards on one symphony's board, over every column. 0 when the board has not been read.
function symphonyCardCount(sym) {
  var snap = sym && sym.state && sym.state.board_snapshot;
  if (!snap || typeof snap !== 'object') return 0;
  var total = 0;
  for (var col in snap) {
    if (symphonyHas(snap, col) && Array.isArray(snap[col])) total += snap[col].length;
  }
  return total;
}

// 155: why a symphony has no check yet, or null once it has been checked, or when the snapshot does
// not say. The kinds are told apart by what the snapshot carries:
//   failed   its first cycle threw (error_count, or last_error); the error is shown, redacted server-side
//   startup  the daemon has not finished a cycle yet, so the symphony may simply not have been reached
//   webhook  polling is off and no cycle has run, so the symphony is waiting for a webhook
//   never    polling is on, the daemon has finished cycles, and this symphony was never checked
// Only "never" is a config question, so only it is greyed out (see symphonyIsInactive).
function symphonyUncheckedNote(sym) {
  if (!sym || typeof sym !== 'object') return null;
  if (!symphonyHas(sym, 'cycle_count') || !symphonyHas(sym, 'last_poll_at')) return null;
  var checked = !!sym.last_poll_at || (typeof sym.cycle_count === 'number' && sym.cycle_count > 0);
  if (checked) return null;
  var lastError = sym.state && typeof sym.state === 'object' ? sym.state.last_error : null;
  if ((typeof sym.error_count === 'number' && sym.error_count > 0) || lastError) {
    return {kind: 'failed', text: 'Last check failed: ' + String(lastError || 'the error was not recorded')};
  }
  var polling = typeof sym.poll_interval_seconds === 'number' && sym.poll_interval_seconds > 0;
  if (!polling) return {kind: 'webhook', text: 'Waiting for a webhook'};
  var daemonCycles = sym.daemon_cycles_completed;
  if (typeof daemonCycles !== 'number' || daemonCycles <= 0) return {kind: 'startup', text: 'Not checked yet'};
  return {kind: 'never', text: SYMPHONY_NEVER_CHECKED_TEXT};
}

// The notes for several symphonies as one line of text, each text once.
function symphonyUncheckedText(syms) {
  var texts = [];
  (syms || []).forEach(function(sym) {
    var note = symphonyUncheckedNote(sym);
    if (note && texts.indexOf(note.text) === -1) texts.push(note.text);
  });
  return texts.join('; ');
}

// Greyed out when the symphony was never checked and holds no cards. Hiding it would hide the
// config problem that keeps it empty. A symphony that is only waiting, or has failed, is not greyed.
function symphonyIsInactive(sym) {
  var note = symphonyUncheckedNote(sym);
  return !!note && note.kind === 'never' && symphonyCardCount(sym) === 0;
}

// 156: a GitHub owner, repository or project number that may be written into a link. Anything
// else is shown as text and never linked, so a bad config value cannot become a URL.
var SYMPHONY_OWNER_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/;
var SYMPHONY_REPO_RE = /^[A-Za-z0-9._-]{1,100}$/;

function symphonyValidOwner(org) {
  return typeof org === 'string' && SYMPHONY_OWNER_RE.test(org);
}

function symphonyValidRepo(repo) {
  return typeof repo === 'string' && SYMPHONY_REPO_RE.test(repo) && repo !== '.' && repo !== '..';
}

function symphonyValidProjectNumber(num) {
  return typeof num === 'number' && isFinite(num) && num >= 1 && Math.floor(num) === num;
}

// The identity of one symphony: "name", "GitHub Project #16" and "owner/repo".
// A part the snapshot does not know is left out, never shown as a placeholder.
function symphonyIdentity(sym) {
  var name = sym && typeof sym.name === 'string' ? sym.name : '';
  var num = sym ? sym.github_project_number : null;
  var hasNum = symphonyValidProjectNumber(num);
  var org = sym && typeof sym.github_org === 'string' ? sym.github_org : '';
  var repo = sym && typeof sym.repo === 'string' ? sym.repo : '';
  var repoName = org && repo ? org + '/' + repo : repo;
  var project = hasNum ? 'GitHub Project #' + num : '';
  var linkOwner = symphonyValidOwner(org);
  return {
    name: name,
    projectText: project,
    projectUrl: hasNum && linkOwner ? 'https://github.com/orgs/' + org + '/projects/' + num : '',
    repoText: repoName,
    repoUrl: linkOwner && symphonyValidRepo(repo) ? 'https://github.com/' + org + '/' + repo : '',
    text: [name, project, repoName].filter(Boolean).join(' · ')
  };
}

// A value as HTML, linked to GitHub only when the link is http(s).
function symphonyLinkHtml(text, url) {
  var href = safeHttpUrl(url);
  return href
    ? '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(text) + '</a>'
    : esc(text);
}

// "#16", linked, for the GitHub Project column. Empty when the number is unknown.
function symphonyProjectHtml(sym) {
  var id = symphonyIdentity(sym);
  return id.projectText ? symphonyLinkHtml('#' + sym.github_project_number, id.projectUrl) : '';
}

// "owner/repo", linked, for a table cell. Empty when the repository is unknown.
function symphonyRepoHtml(sym) {
  var id = symphonyIdentity(sym);
  return id.repoText ? symphonyLinkHtml(id.repoText, id.repoUrl) : '';
}

// The identity line as HTML: every value escaped, and each link only when it is http(s).
function symphonyIdentityHtml(sym) {
  var id = symphonyIdentity(sym);
  var parts = [esc(id.name)];
  if (id.projectText) parts.push(symphonyLinkHtml(id.projectText, id.projectUrl));
  if (id.repoText) parts.push(symphonyLinkHtml(id.repoText, id.repoUrl));
  return parts.filter(Boolean).join(' · ');
}

// --- the Symphonies table (156) ------------------------------------------------------ #
// Every column explains itself or links to more detail. The functions below build one row
// from one symphony's snapshot entry; renderSymphoniesPage (app.js) writes the table.

// The Status column: Disabled, Error, Paused or Active, with the reason. A disabled symphony is
// not checked, so it comes first. An Error needs a person, so it comes next, then cards the board
// paused. A never-checked symphony's reason is the #155 note, not a second classification.
var SYMPHONY_STATUS_COLORS = {
  muted: 'var(--color-text-muted)',
  green: 'var(--color-accent-green)',
  red: 'var(--color-accent-red)',
  blue: 'var(--color-accent-blue)'
};

function symphonyStatus(sym) {
  if (!sym || typeof sym !== 'object') {
    return {label: 'Active', reason: 'The snapshot does not carry this symphony.', tone: 'muted'};
  }
  var note = symphonyUncheckedNote(sym);
  // 101: a branch review the daemon identity cannot give is said here, in the Active reason.
  var review = sym.merge_review && typeof sym.merge_review === 'object' ? sym.merge_review : null;
  // Only a requirement that holds, or one that cannot be read, is a warning. A bypassed or clear branch says nothing.
  var reviewWarns = !!(review && review.message && (review.state === 'external_review_required' || review.state === 'unreadable'));
  var lastError = sym.state && typeof sym.state === 'object' && sym.state.last_error ? String(sym.state.last_error) : '';
  var label;
  var reason;
  if (sym.enabled === false) {
    label = 'Disabled';
    reason = 'enabled: false in the config, so coordinare does not check this symphony.';
  } else if ((note && note.kind === 'failed') || (typeof sym.error_count === 'number' && sym.error_count > 0)) {
    label = 'Error';
    if (note && note.kind === 'failed') reason = note.text;
    else if (lastError) reason = 'Last check failed: ' + lastError;
    else reason = sym.error_count + ' checks failed in a row; the error was not recorded.';
  } else if (typeof sym.cards_paused === 'number' && sym.cards_paused > 0) {
    label = 'Paused';
    reason = sym.cards_paused + (sym.cards_paused === 1 ? ' card is' : ' cards are') + ' paused on the board.';
  } else {
    label = 'Active';
    if (reviewWarns) reason = String(review.message);
    else if (note) reason = note.text;
    else if (typeof sym.poll_interval_seconds === 'number' && sym.poll_interval_seconds > 0) {
      reason = 'Checked every ' + sym.poll_interval_seconds + ' seconds.';
    } else reason = 'Checked when a webhook arrives.';
  }
  // 101: a paused or failed symphony still has the branch condition, so it is added after the reason.
  if (reviewWarns && (label === 'Error' || label === 'Paused')) {
    reason = reason + ' ' + String(review.message);
  }
  var tone = {Disabled: 'muted', Error: 'red', Paused: 'blue', Active: 'green'}[label];
  return {label: label, reason: reason, tone: tone};
}

// The Env column's words for each env state the server reports (156).
var SYMPHONY_ENV_LABELS = {
  not_configured: 'Not configured',
  bootstrapping: 'Bootstrapping',
  failed: 'Failed',
  ready: 'Ready',
  not_bootstrapped: 'Not bootstrapped'
};

// 266: the env bootstrap in words. The server derives one state per symphony (services/bootstrap_status.py);
// the Symphonies page shows its state, its figures and its recent attempts, and says nothing it was not sent.
var SYMPHONY_BOOTSTRAP_LABELS = {
  not_configured: 'Not configured', not_started: 'Not started', bootstrapping: 'Bootstrapping',
  verifying: 'Verifying', ready: 'Ready', failed: 'Failed', waiting_to_retry: 'Waiting to retry',
  budget_exhausted: 'Budget exhausted', passed_not_ready: 'Passed, not ready'
};
var SECONDS_PER_MINUTE = 60;
var BOOTSTRAP_OUTCOME_LABELS = {succeeded: 'Succeeded', failed: 'Failed', running: 'Running', interrupted: 'Interrupted'};

// The elapsed time of a bootstrap, in the words the cell uses: seconds, then whole minutes.
function bootstrapElapsedText(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '';
  if (seconds < SECONDS_PER_MINUTE) return Math.floor(seconds) + 's';
  return Math.floor(seconds / SECONDS_PER_MINUTE) + 'm';
}

// One line of a bootstrap's figures, each one present only when the snapshot carries it.
function bootstrapFigures(parts) {
  return parts.filter(function(p) { return !!p; }).join(' · ');
}

// The verify output tail, in a closed disclosure so a long log does not fill the cell.
function bootstrapTailHtml(boot) {
  var tail = Array.isArray(boot.reason_tail) ? boot.reason_tail : [];
  if (!tail.length) return '';
  return '<details class="sym-boot-tail"><summary>Verify output</summary><pre style="white-space:pre-wrap;font-size:11px">' +
    esc(tail.join('\n')) + '</pre></details>';
}

// One run in the history disclosure: its number, commit, start, how it ended and why.
function bootstrapHistoryRowHtml(a, name) {
  var outcome = symphonyHas(BOOTSTRAP_OUTCOME_LABELS, a.outcome) ? BOOTSTRAP_OUTCOME_LABELS[a.outcome] : 'Unknown';
  var when = a.started_at ? timeHtml(a.started_at) : 'time not recorded';
  var took = bootstrapElapsedText(a.duration_seconds);
  var model = [a.backend, a.model].filter(function(p) { return !!p; }).join(' ');
  var link = a.run_id && name ? '<a class="sym-boot-run-link" href="' +
    esc('/symphonies/' + encodeURIComponent(name) + '/bootstrap/' + encodeURIComponent(a.run_id)) +
    '" onclick="navigate(event,this.pathname)">Open run</a> · ' : '';
  return '<li>' + link + '#' + esc(String(a.number)) + ' · ' + esc(a.commit || 'no commit') + ' · ' + when +
    ' · ' + esc(outcome) + (took ? ' after ' + esc(took) : '') +
    (a.reason ? '<div class="muted" style="font-size:11px">' + esc(a.reason) + '</div>' : '') +
    (model ? '<div class="muted" style="font-size:11px">' + esc(model) + (a.job_id ? ' · job ' + esc(a.job_id) : '') + '</div>' : '') +
    '</li>';
}

// The bootstrap's detail under the Env cell: what it is doing or why it stopped, and its recent runs.
// A hidden row gets none of it, as the reason and the runs are the symphony's own.
function symphonyBootstrapHtml(boot, name) {
  if (!boot || typeof boot !== 'object' || !symphonyHas(SYMPHONY_BOOTSTRAP_LABELS, boot.state)) return '';
  var lines = [];
  var state = boot.state;
  if (state === 'bootstrapping' || state === 'verifying') {
    lines.push(bootstrapFigures([
      boot.started_at ? 'Started ' + timeHtml(boot.started_at) : '',
      bootstrapElapsedText(boot.elapsed_seconds) ? 'elapsed ' + bootstrapElapsedText(boot.elapsed_seconds) : '',
      boot.backend ? esc([boot.backend, boot.model].filter(function(p) { return !!p; }).join(' ')) : '',
      boot.pod ? 'Pod ' + esc(boot.pod) : ''
    ]));
  }
  if (state === 'ready' && boot.ready_since) lines.push('Since ' + timeHtml(boot.ready_since));
  if (state === 'waiting_to_retry' && boot.next_attempt_at) lines.push('Next run ' + timeHtml(boot.next_attempt_at));
  if (state === 'failed' || state === 'waiting_to_retry' || state === 'budget_exhausted') {
    if (boot.reason) lines.push('<span style="color:var(--color-text)">' + esc(boot.reason) + '</span>');
    if (boot.reset_hint) lines.push(esc(boot.reset_hint));
  }
  var out = lines.map(function(l) { return '<div class="muted" style="font-size:11px">' + l + '</div>'; }).join('');
  var latest = boot.run_id || (boot.history && boot.history.length ? boot.history[0].run_id : '');
  if (latest && name) out += '<a class="sym-boot-run-link" href="' +
    esc('/symphonies/' + encodeURIComponent(name) + '/bootstrap/' + encodeURIComponent(latest)) +
    '" onclick="navigate(event,this.pathname)">Open bootstrap run</a>';
  out += bootstrapTailHtml(boot);
  var history = Array.isArray(boot.history) ? boot.history : [];
  if (history.length) {
    out += '<details class="sym-boot-history"><summary class="muted" style="font-size:11px">History (' +
      esc(String(history.length)) + ')</summary><ul style="font-size:11px;padding-left:16px">' +
      history.map(function(a) { return bootstrapHistoryRowHtml(a, name); }).join('') + '</ul></details>';
  }
  return out;
}

function symphonyEnvView(sym) {
  // 266: the bootstrap's own state, when the snapshot carries it.
  var boot = sym && sym.env_bootstrap && typeof sym.env_bootstrap === 'object' ? sym.env_bootstrap : null;
  if (boot && symphonyHas(SYMPHONY_BOOTSTRAP_LABELS, boot.state)) {
    return {label: SYMPHONY_BOOTSTRAP_LABELS[boot.state], reason: String(boot.label || '')};
  }
  var env = sym && sym.env_status && typeof sym.env_status === 'object' ? sym.env_status : null;
  if (!env || !symphonyHas(SYMPHONY_ENV_LABELS, env.state)) {
    return {label: 'Not known', reason: "The snapshot does not carry this symphony's env state."};
  }
  return {label: SYMPHONY_ENV_LABELS[env.state], reason: String(env.reason || '')};
}

// Under a chosen symphony, every other row's reasons, spend and card cells are hidden, so the
// figures and error text of a symphony the viewer did not choose stay out of view. The row itself
// stays, as the page lists every symphony by design, and the status word and env word stay too.
//
// This is a display filter, not a data boundary. The snapshot carries every symphony's data to
// every browser (the Symphonies page lists them all), so a determined reader of the page's
// scripts can still see it. Making scope a data boundary needs the server to omit other symphonies.
function symphonyRowHidden(name, choice) {
  return !!choice && choice !== name;
}

// A count for a table cell, or "Not known" when the snapshot does not carry it.
function symphonyCountText(value) {
  return typeof value === 'number' && isFinite(value) ? String(value) : 'Not known';
}

// One row of the Symphonies table. Every value is escaped. The row opens the symphony's page
// (renderSymphoniesPage wires the click). choice is the symphony the header chose, or '' for All.
function symphonyTableRowHtml(sym, choice) {
  var status = symphonyStatus(sym);
  var env = symphonyEnvView(sym);
  var hidden = symphonyRowHidden(sym.name, choice);
  var hiddenHtml = '<span class="muted" style="font-size:11px">Hidden: another symphony is shown</span>';
  var unchecked = symphonyUncheckedNote(sym);
  // 157: the last check is a relative time with the exact time and zone on hover. A hidden row shows the marker, not the time.
  var checked = sym.last_poll_at ? timeHtml(sym.last_poll_at) : '';
  var href = '/symphonies/' + encodeURIComponent(sym.name);
  var running = sym.env_bootstrap && ['bootstrapping', 'verifying'].indexOf(sym.env_bootstrap.state) !== -1;
  var bootBtn = sym.env_bootstrap_performer_id && !hidden
    ? '<div><button class="action-btn sym-row-bootstrap" data-sym="' + esc(sym.name) + '">Retry now</button>' +
      (running ? ' <button class="action-btn sym-row-bootstrap-cancel" data-sym="' + esc(sym.name) + '">Cancel</button>' : '') + '</div>'
    : '';
  var inactive = symphonyIsInactive(sym);
  return '<tr class="symphony-row' + (inactive ? ' symphony-inactive' : '') + '"' + (inactive ? ' data-inactive="true"' : '') + ' data-href="' + esc(href) + '">'
    + '<td><a href="' + esc(href) + '" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue)">' + esc(sym.name) + '</a></td>'
    + '<td><span style="color:' + SYMPHONY_STATUS_COLORS[status.tone] + ';font-weight:bold">' + esc(status.label) + '</span>'
    + '<div class="muted" style="font-size:11px">' + (hidden ? hiddenHtml : esc(status.reason)) + '</div></td>'
    + '<td style="color:var(--color-text-muted)">' + (symphonyProjectHtml(sym) || 'No project number') + '</td>'
    + '<td style="color:var(--color-text-muted)">' + (symphonyRepoHtml(sym) || 'No repository') + '</td>'
    + '<td>' + esc(env.label) + '<div class="muted" style="font-size:11px">' + (hidden ? hiddenHtml : esc(env.reason)) + '</div>' +
    (hidden ? '' : symphonyBootstrapHtml(sym.env_bootstrap, sym.name)) + bootBtn + '</td>'
    + '<td>' + (hidden ? hiddenHtml : esc(symphonyCountText(sym.cards_active))) + '</td>'
    + '<td>' + (hidden ? hiddenHtml : esc(symphonyCountText(sym.cards_queued))) + '</td>'
    + '<td>' + (hidden ? hiddenHtml : usageCostHtml(sym.spend_week)) + '</td>'
    + '<td>' + esc(symphonyCountText(sym.cycle_count)) + '</td>'
    + '<td>' + esc(symphonyCountText(sym.error_count)) + '</td>'
    // The unchecked note can carry the last error, so under a scope it is hidden with the rest.
    + '<td>' + (hidden ? hiddenHtml : (checked || (unchecked ? esc(unchecked.text) : 'Not checked yet'))) + '</td>'
    + '</tr>';
}

// The header control as HTML. One symphony: its identity, no switcher. Several:
// a select with All symphonies first, then the identity of the one chosen.
function symphonySwitcherHtml(s, choice) {
  var names = symphonyNames(s);
  if (names.length === 0) return '';
  if (names.length === 1) {
    return '<span id="symphony-identity">' + symphonyIdentityHtml(symphonyEntry(s, names[0])) + '</span>';
  }
  var options = '<option value="' + esc(SYMPHONY_ALL) + '"' + (choice === SYMPHONY_ALL ? ' selected' : '') + '>All symphonies</option>';
  names.forEach(function(name) {
    // 155: a select cannot grey out one option, so an unchecked symphony says why in its label.
    var unchecked = symphonyUncheckedNote(symphonyEntry(s, name));
    var note = unchecked ? ' (' + unchecked.text + ')' : '';
    options += '<option value="' + esc(name) + '"' + (name === choice ? ' selected' : '') + '>' + esc(name + note) + '</option>';
  });
  var identity = choice === SYMPHONY_ALL ? '' : symphonyIdentityHtml(symphonyEntry(s, choice));
  return '<label for="symphony-select" class="muted">Symphony</label>' +
    '<select id="symphony-select" onchange="onSymphonyChoice(this.value)">' + options + '</select>' +
    '<span id="symphony-identity" class="muted">' + identity + '</span>';
}

// --- the page ----------------------------------------------------------------- #
// Everything below reads the browser or writes the page. The helpers above do not.

var _symphonySwitcherHtml = null;   // the last header markup written, so a live update does not rebuild the select under the user

function readStoredSymphony() {
  try { return window.localStorage.getItem(SYMPHONY_STORAGE_KEY) || ''; } catch (e) { return ''; }
}

function writeStoredSymphony(choice) {
  // Private browsing can refuse storage. The choice then lasts for this page only.
  try { window.localStorage.setItem(SYMPHONY_STORAGE_KEY, choice); } catch (e) { return; }
}

// The choice in force for a snapshot: a valid deep link, else the stored choice, else All.
function currentSymphonyChoice(s) {
  var fromUrl = symphonyParamFromSearch(typeof location !== 'undefined' ? location.search : '');
  return resolveSymphonyChoice(fromUrl, readStoredSymphony(), symphonyNames(s));
}

// 135: whether the page is showing one symphony rather than All. The activity feed is
// deployment-wide, so its dropped-entries line says it covers every symphony when this is true.
function symphonyChoiceForNote() {
  var s = typeof _lastState !== 'undefined' ? _lastState : null;
  return currentSymphonyChoice(s) !== SYMPHONY_ALL;
}

// The snapshot every panel should render: the full snapshot, narrowed to the choice.
function scopedView(s) {
  return symphonyScope(s, currentSymphonyChoice(s));
}

// Writes the header control, and remembers a deep-linked choice so it survives navigation.
function renderSymphonySwitcher() {
  var el = document.getElementById('project-link');
  if (!el) return;
  var s = typeof _lastState !== 'undefined' ? _lastState : null;
  var choice = currentSymphonyChoice(s);
  var fromUrl = symphonyParamFromSearch(location.search);
  if (fromUrl && fromUrl === choice && readStoredSymphony() !== choice) writeStoredSymphony(choice);
  var html = s ? symphonySwitcherHtml(s, choice) : '';
  el.style.display = html ? '' : 'none';
  if (html === _symphonySwitcherHtml) return;
  _symphonySwitcherHtml = html;
  el.innerHTML = html;
}

// The select's onchange. Stores the choice, puts it in the address bar so the
// page can be bookmarked, and re-renders every view that is scoped.
function onSymphonyChoice(value) {
  var choice = resolveSymphonyChoice(value, '', symphonyNames(typeof _lastState !== 'undefined' ? _lastState : null));
  writeStoredSymphony(choice);
  var query = choice === SYMPHONY_ALL ? '' : '?' + SYMPHONY_PARAM + '=' + encodeURIComponent(choice);
  history.replaceState(history.state, '', location.pathname + query + location.hash);
  refreshScopedViews();
}

// Re-renders the views that the choice changes, from the last snapshot.
function refreshScopedViews() {
  renderSymphonySwitcher();
  if (typeof _lastState === 'undefined' || !_lastState) return;
  var scoped = scopedView(_lastState);
  updateAttention(scoped);
  renderState(scoped);
  if (isPerformersPath(location.pathname)) renderPerformersPage(scoped);
  else if (location.pathname === '/logs') renderLogsPage(currentScopedState());
  else if (isBootstrapRunPath(location.pathname)) bootstrapPageRefresh();
  else if (isCardPath(location.pathname)) cardPageRefresh();
  else renderDashboardExtras(scoped);
  afUpdateFilterOptions();
  afRender();
}

// The address for an in-app link: the same path, carrying the symphony the header
// has chosen, so navigating does not drop the scope. A path that already has a
// query, or a deployment with one symphony, is returned as it is.
function pathWithSymphony(path) {
  var target = String(path || '');
  // 143 second review: the query belongs before the hash, so a deep link keeps its hash.
  var hash = '';
  var at = target.indexOf('#');
  if (at !== -1) { hash = target.slice(at); target = target.slice(0, at); }
  if (target.indexOf('?') !== -1 || typeof _lastState === 'undefined' || !_lastState) return target + hash;
  if (symphonyNames(_lastState).length < 2) return target + hash;
  var choice = currentSymphonyChoice(_lastState);
  if (!choice) return target + hash;
  return target + '?' + SYMPHONY_PARAM + '=' + encodeURIComponent(choice) + hash;
}
