// 349: shared dashboard helpers, served verbatim from /static/helpers.js.
// The page pins the sha256 of these bytes into the script-tag ?v= query, so a
// redeploy that changes the file changes the URL and no browser ever renders
// a stale copy from cache.
function fmtDuration(secs) {
  if (secs === null || secs === undefined) return '—';
  return secs.toFixed(2) + 's';
}

function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

function fmtAge(iso) {
  if (!iso) return null;
  var secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

// 136: every display label for an internal code comes from the glossary
// (/static/vocabulary.js, generated from coordinare/vocabulary.py). A code
// with no entry is shown as it is, so a missing label is visible, not
// papered over by title-casing.
function vocabularyLabel(code) {
  var key = String(code || '');
  var known = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY.labels) || {};
  return Object.prototype.hasOwnProperty.call(known, key) ? known[key] : key;
}

function humanPhase(phase) {
  return esc(vocabularyLabel(phase));
}

// 137: a stage says what is happening ("Implement"), an agent says who does it
// ("Implementer"). Several codes are both (security, qa), with different
// labels, so each is looked up by kind and never through humanPhase.
function vocabularyKindLabel(kind, code) {
  var key = String(code || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var known = v[kind] || {};
  return Object.prototype.hasOwnProperty.call(known, key) ? known[key] : key;
}

function stageLabel(stage) {
  return esc(vocabularyKindLabel('stage_labels', stage));
}

function agentLabel(role) {
  return esc(vocabularyKindLabel('agent_labels', role));
}

// The agent code that does a stage, or '' when the stage has none.
function agentForStage(stage) {
  var key = String(stage || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var map = v.stage_agent || {};
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : '';
}

// "Implement · Implementer": a stage shown with the agent that does it.
function stageAgentLabel(stage) {
  var agent = agentForStage(stage);
  var known = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY && COORDINARE_VOCABULARY.stage_labels) || {};
  if (!agent || !Object.prototype.hasOwnProperty.call(known, String(stage || ''))) return stageLabel(stage);
  return stageLabel(stage) + ' · ' + agentLabel(agent);
}

// 137: the Personas page says which stage each agent serves.
function personaStageNote(role) {
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var map = v.stage_agent || {};
  for (var stage in map) {
    if (Object.prototype.hasOwnProperty.call(map, stage) && map[stage] === String(role || '')) {
      return ' <span class="muted" style="font-weight:normal">· ' + stageLabel(stage) + ' stage</span>';
    }
  }
  return ' <span class="muted" style="font-weight:normal">· no pipeline stage</span>';
}

// A role code may be written as an agent ("implementer") or as the stage it
// serves ("implementing"); show the pair either way.
function roleOrStageLabel(code) {
  var key = String(code || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var stages = v.stage_labels || {};
  if (Object.prototype.hasOwnProperty.call(stages, key)) return stageAgentLabel(key);
  var map = v.stage_agent || {};
  for (var stage in map) {
    if (Object.prototype.hasOwnProperty.call(map, stage) && map[stage] === key) return stageAgentLabel(stage);
  }
  return agentLabel(key);
}

// 144: one cost format everywhere: two decimals, three when under a cent.
function fmtCost(value) {
  var n = Number(value);
  if (!isFinite(n) || n <= 0) return '$0.00';
  return '$' + (n < 0.01 ? n.toFixed(3) : n.toFixed(2));
}

// 144: "1 of 1 busy", plus the queue when there is one. Idle was only ever
// max minus active, so it is not shown.
function busyText(active, max, queued) {
  var a = Number(active) || 0;
  var m = Number(max) || 0;
  var q = Number(queued) || 0;
  return a + ' of ' + m + ' busy' + (q > 0 ? ' · ' + q + ' queued' : '');
}

// 138: the conductor headline as a plain sentence. `s` is the dashboard
// state; `activeSess` is the session an agent is running for, if any.
function cardRefText(sess) {
  if (!sess) return '';
  if (sess.issue_number) return '#' + sess.issue_number;
  return String(sess.card_title || '');
}

function conductorHeadline(s, activeSess) {
  s = s || {};
  if (activeSess) {
    var who = vocabularyKindLabel('agent_labels', agentForStage(activeSess.performer_stage)) || 'an agent';
    var on = cardRefText(activeSess);
    return 'Watching ' + who + (on ? ' on ' + on : '');
  }
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  for (var i = 0; i < sessions.length; i++) {
    if (sessions[i].phase === 'dispatching') {
      var agent = vocabularyKindLabel('agent_labels', agentForStage(sessions[i].performer_stage));
      var card = cardRefText(sessions[i]);
      return 'Dispatching' + (card ? ' ' + card : '') + (agent ? ' to ' + agent : '');
    }
  }
  var phase = String(s.phase || 'idle');
  if (phase === 'idle') {
    var wait = nextCheckSeconds(s);
    return wait == null ? 'Idle: waiting for the next check' : 'Idle: next check in ' + wait + 's';
  }
  // The server's label is the glossary label for this state; fall back to a
  // local lookup when a snapshot carries none.
  return String(s.phase_label || vocabularyLabel(phase));
}

// The headline carries a countdown ("next check in 18s"), and state arrives
// only once per check, so the page's one-second timer re-renders it.
function refreshConductorHeadline() {
  var el = document.getElementById('phase');
  // 139: the headline reads the symphony the header has chosen, like every other panel.
  var state = typeof currentScopedState === 'function' ? currentScopedState() : null;
  if (!el || !state) return;
  var active = typeof findActivePerformerSession === 'function' ? findActivePerformerSession(state) : null;
  var text = conductorHeadline(state, active);
  if (el.textContent !== text) el.textContent = text;
}

// Seconds until the next board check, or null when it cannot be known.
function nextCheckSeconds(s, now) {
  var interval = Number(s && s.poll_interval_seconds);
  var last = s && s.last_poll_at ? new Date(s.last_poll_at).getTime() : NaN;
  if (!isFinite(interval) || interval <= 0 || !isFinite(last)) return null;
  return Math.max(0, Math.ceil((last + interval * 1000 - (now == null ? Date.now() : now)) / 1000));
}

// 152: how fresh the data on screen is, beside the header's connection label.
// "Live · updated 3s ago · next check in 18s", or, when the stream is quiet or
// reconnecting, "Updates quiet · no update for 2m".
function fmtShortAge(ms) {
  var secs = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

function freshnessText(label, lastMessageAt, hasMessage, s, now) {
  if (!hasMessage) return '';
  var age = fmtShortAge(now - lastMessageAt);
  if (label === 'Live') {
    var next = nextCheckSeconds(s, now);
    return ' · updated ' + age + ' ago' + (next == null ? '' : ' · next check in ' + next + 's');
  }
  return ' · no update for ' + age + (label === 'Reconnecting' ? ': reconnecting…' : '');
}

// Called once a second. Reads the page's own connection bookkeeping.
// When the last state update arrived; set by the state_update listener.
var _lastStateAt = 0;

function refreshFreshness() {
  var detail = document.getElementById('connection-detail');
  var labelEl = document.getElementById('connection-label');
  if (!detail || !labelEl) return;
  var label = labelEl.textContent;
  // "updated Ns ago" is the age of the data on screen: the last state update,
  // not the last heartbeat. Heartbeats arrive every few seconds and only show
  // that the stream is open; they are what the Live label is for.
  var stateAt = Number(_lastStateAt) || 0;
  var has = stateAt > 0;
  var last = stateAt;
  // The poll interval the freshness line counts down is the selected symphony's (139).
  var state = typeof currentScopedState === 'function' ? currentScopedState() : null;
  detail.textContent = freshnessText(label, last, has, state, Date.now());
  var status = document.getElementById('connection-status');
  if (status) status.classList.toggle('conn-stale', has && label !== 'Live');
}

// 136: the Glossary page, rendered from the same data.
function renderGlossaryPage() {
  var el = document.getElementById('glossary-section');
  if (!el) return;
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  function table(title, terms) {
    if (!Array.isArray(terms) || terms.length === 0) return '';
    return '<h3>' + esc(title) + '</h3><table><tbody>' + terms.map(function(t) {
      return '<tr><td style="white-space:nowrap"><strong>' + esc(t.label) + '</strong></td><td>' + esc(t.description) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }
  el.innerHTML = table('Concepts', v.concepts) + table('Stages', v.stages) + table('Agents', v.agents) +
    table('Conductor status', v.phases) + table('Check outcomes', v.outcomes);
}
var formatPhaseLabel = humanPhase;

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// 141: a run is named the same way on every screen: "run" and the first six characters of
// its id. A prefix ending in an underscore ("run_", "sess_") is not part of the short name.
// The full id is in the title, and the Copy button copies it.
function runIdText(runId) {
  var bare = String(runId || '').replace(/^[A-Za-z][A-Za-z0-9]*_/, '');
  var head = bare.slice(0, 6);
  return head ? 'run ' + head : '';
}

// 141: a run's name as markup. opts.href makes the short name a link (opts.blank opens it in a
// new tab, opts.className styles it); opts.copy adds the Copy button. Every value is escaped here.
function runIdHtml(runId, opts) {
  opts = opts || {};
  var full = String(runId || '');
  var short = runIdText(full);
  if (!short) return '<span class="muted">-</span>';
  var title = esc(full);
  var name;
  if (opts.href) {
    var cls = ' class="' + esc(opts.className || 'run-id-link') + '"';
    var attrs = ' href="' + esc(opts.href) + '" title="' + title + '"' + cls;
    attrs += opts.blank
      ? ' target="_blank" rel="noopener"'
      : ' onclick="event.stopPropagation();navigate(event,this.getAttribute(\'href\'))" onkeydown="event.stopPropagation()"';
    name = '<a' + attrs + '>' + esc(short) + '</a>';
  } else {
    name = '<span class="run-id" title="' + title + '">' + esc(short) + '</span>';
  }
  var copy = opts.copy
    ? ' <button type="button" class="run-id-copy" data-run-id="' + title + '" title="' + title + '"' +
      ' onclick="event.stopPropagation();copyRunId(this)" onkeydown="event.stopPropagation()">Copy</button>'
    : '';
  return name + copy;
}

// 141: copies a run's full id. The clipboard API is used when the page may use it. Otherwise,
// or when the browser refuses it (Firefox does without a grant), the id is copied through a
// selected field instead. The button says what happened, and the title still holds the id.
function copyRunId(btn) {
  var full = btn ? btn.getAttribute('data-run-id') || '' : '';
  if (!full) return;
  var report = function(ok) {
    btn.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
  };
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    navigator.clipboard.writeText(full).then(function() { report(true); }, function() { report(runCopyBySelection(full)); });
    return;
  }
  report(runCopyBySelection(full));
}

// 141: the fallback copy. The id is selected in a field that is never shown, then the browser's
// copy command runs on that selection.
function runCopyBySelection(text) {
  var field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.position = 'fixed';
  field.style.opacity = '0';
  document.body.appendChild(field);
  field.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(field);
  return ok;
}

// 141: "Run 2 of Implement", shown only when the daemon has counted the run's place in its stage.
// Nothing in the snapshot counts runs per stage, so a caller without a count gets ''.
function runNumberText(stage, attempt) {
  var n = Number(attempt);
  if (!(n >= 1) || n !== Math.floor(n)) return '';
  return 'Run ' + n + ' of ' + stageLabel(stage);
}

function fmtBytes(b) {
  if (b == null) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(0) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}
