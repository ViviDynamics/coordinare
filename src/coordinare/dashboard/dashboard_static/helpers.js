// 349: shared dashboard helpers, served verbatim from /static/helpers.js.
// The page pins the sha256 of these bytes into the script-tag ?v= query, so a
// redeploy that changes the file changes the URL and no browser ever renders
// a stale copy from cache.
function fmtDuration(secs) {
  if (secs === null || secs === undefined) return 'unknown';
  return secs.toFixed(2) + 's';
}

// 157: the one parse for every dashboard timestamp. The daemon writes ISO 8601 strings. A string with
// no zone designator is UTC, as the daemon means it, and so is read as UTC (the moment is then shown in
// the viewer's zone, labelled). Microseconds are dropped first, because not every browser reads them.
// Only strings are timestamps here: a number (epoch seconds or milliseconds) reads as unreadable.
function timeMs(iso) {
  if (typeof iso !== 'string' || iso === '') return NaN;
  // Fractions are kept to the millisecond: not every browser reads six digits, and a dropped fraction moves an age.
  var text = iso.replace(/\.(\d+)/, function(m, digits) { return '.' + (digits + '000').slice(0, 3); }).replace(' ', 'T');
  if (!/(Z|[+-]\d\d:?\d\d)$/i.test(text)) text += 'Z';
  return Date.parse(text);
}

// 157: a timestamp in milliseconds, or NaN. Kept for the callers that name it; it is timeMs.
function isoMs(iso) {
  return timeMs(iso);
}

// 157: the zone's short name ("EDT") for a moment in the viewer's zone, or '' when the browser names none.
// One formatter serves the page. It is rebuilt when the browser's time zone changes, so a laptop that
// travels is labelled correctly without a reload.
function zoneLabel(d) {
  try {
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zoneLabel.formatter || zoneLabel.zone !== zone) {
      zoneLabel.formatter = new Intl.DateTimeFormat('en-US', {timeZoneName: 'short'});
      zoneLabel.zone = zone;
    }
    var parts = zoneLabel.formatter.formatToParts(d);
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].type === 'timeZoneName' && parts[i].value) return parts[i].value;
    }
  } catch(e) { /* no Intl: the caller shows UTC */ }
  return '';
}

// 157: the exact time with its zone, "2026-10-13 17:00:50 EDT". With no zone name the time is shown in
// UTC and labelled UTC. An unreadable timestamp is "unknown". The relative text is fmtAgo's.
function fmtTime(iso) {
  var at = timeMs(iso);
  if (isNaN(at)) return 'unknown';
  var d = new Date(at);
  var zone = zoneLabel(d);
  var u = zone === '';
  var two = function(n) { return (n < 10 ? '0' : '') + n; };
  var y = u ? d.getUTCFullYear() : d.getFullYear();
  var mo = (u ? d.getUTCMonth() : d.getMonth()) + 1;
  var day = u ? d.getUTCDate() : d.getDate();
  var h = u ? d.getUTCHours() : d.getHours();
  var mi = u ? d.getUTCMinutes() : d.getMinutes();
  var s = u ? d.getUTCSeconds() : d.getSeconds();
  return y + '-' + two(mo) + '-' + two(day) + ' ' + two(h) + ':' + two(mi) + ':' + two(s) + ' ' + (u ? 'UTC' : zone);
}

// 157: the one relative age: "35s ago", "12m ago", "3h ago", "2d ago". '' when the timestamp is unreadable.
// A moment after nowMs reads as "0s ago". Every relative time on the dashboard is this text.
function fmtAgo(iso, nowMs) {
  var at = timeMs(iso);
  if (isNaN(at)) return '';
  var now = nowMs == null ? Date.now() : nowMs;
  var secs = Math.max(0, Math.floor((now - at) / 1000));
  if (secs < 60) return secs + 's ago';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  var hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  return Math.floor(hours / 24) + 'd ago';
}

// The names older callers and tests use. Each is fmtAgo: one implementation, one format.
function fmtAge(iso) {
  return fmtAgo(iso);
}

function fmtAgeAt(iso, nowMs) {
  return fmtAgo(iso, nowMs);
}

// 157: the one way a dashboard page shows an absolute time: the relative age as the text, the exact
// time with its zone on hover. refreshRelativeTimes keeps the age current. Every absolute time goes here.
function timeHtml(iso, fallback) {
  var exact = fmtTime(iso);
  if (exact === 'unknown') return esc(fallback || 'unknown');
  return '<time title="' + esc(exact) + '" data-rel-iso="' + esc(String(iso)) + '">' + esc(fmtAgo(iso)) + '</time>';
}

// 157: re-reads the age of every timeHtml time under root. Called once a second with the other readouts.
function refreshRelativeTimes(root) {
  var nodes = (root || document).querySelectorAll('time[data-rel-iso]');
  for (var i = 0; i < nodes.length; i++) {
    var text = fmtAgo(nodes[i].getAttribute('data-rel-iso'));
    if (text && nodes[i].textContent !== text) nodes[i].textContent = text;
  }
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
  // 154: the countdown is the freshness line's (152), so the headline says only the state.
  if (phase === 'idle') return 'Idle: waiting for the next check';
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
// reconnecting, "Updates quiet · last update 2m ago".
function freshnessText(label, lastMessageAt, hasMessage, s, now) {
  if (!hasMessage) return '';
  var age = fmtAgo(new Date(lastMessageAt).toISOString(), now);
  if (label === 'Live') {
    var next = nextCheckSeconds(s, now);
    return ' · updated ' + age + (next == null ? '' : ' · next check in ' + next + 's');
  }
  return ' · last update ' + age + (label === 'Reconnecting' ? ': reconnecting…' : '');
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
  // Only the run addresses are links. Any other href renders the name without one.
  var href = typeof opts.href === 'string' ? opts.href : '';
  var linkable = href.indexOf('/runs/') === 0 || href.indexOf('/api/runs/') === 0;
  if (linkable) {
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
    navigator.clipboard.writeText(full).then(function() { report(true); }, function() { report(runCopyBySelection(full, btn)); });
    return;
  }
  report(runCopyBySelection(full, btn));
}

// 141: the fallback copy. The id is selected in a field that is never shown, then the browser's
// copy command runs on that selection. Removing the field drops focus to the body, so focus goes
// back to the Copy button (back), where a keyboard or screen-reader user left it.
function runCopyBySelection(text, back) {
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
  if (back && typeof back.focus === 'function') back.focus();
  return ok;
}

// 141 review: a count of tries, singular for one. Used where the env-cache line counts bootstrap tries.
function tryCountText(count) {
  return count === 1 ? '1 try' : String(count) + ' tries';
}

// 141: "Run 2 of Implement", shown only when the daemon has counted the run's place in its stage.
// Nothing in the snapshot counts runs per stage, so a caller without a count gets ''.
function runNumberText(stage, attempt) {
  var n = Number(attempt);
  if (!(n >= 1) || n !== Math.floor(n)) return '';
  return 'Run ' + n + ' of ' + stageLabel(stage);
}

function fmtBytes(b) {
  if (b == null) return 'unknown';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(0) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}
