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
// event), so it stays under every choice.
function symphonyKeepsCard(s, choice, cardId) {
  if (!choice || !cardId) return true;
  return symphonyOfCard(s, cardId) === choice;
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
function symphonyScope(s, choice) {
  if (!s || !choice || symphonyNames(s).length < 2) return s;
  var scoped = {};
  for (var key in s) {
    if (symphonyHas(s, key)) scoped[key] = s[key];
  }
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var questions = Array.isArray(s.open_questions) ? s.open_questions : [];
  scoped.active_sessions = sessions.filter(function(sess) {
    return !!sess && symphonyOfCard(s, sess.card_id) === choice;
  });
  scoped.open_questions = questions.filter(function(q) {
    return !q || typeof q !== 'object' || symphonyKeepsCard(s, choice, q.card_id);
  });
  scoped.symphonies = (Array.isArray(s.symphonies) ? s.symphonies : []).filter(function(sym) {
    return !!sym && sym.name === choice;
  });
  scoped.board_summary = symphonyBoardSummary(scoped.symphonies);
  scoped.token_usage = symphonySpend(s.token_usage, choice);
  return scoped;
}

// Only an https link is ever written into the page.
function safeHttpUrl(url) {
  var text = String(url || '');
  return /^https?:\/\//i.test(text) ? text : '';
}

// The identity of one symphony: "name", "GitHub Project #16" and "owner/repo".
// A part the snapshot does not know is left out, never shown as a placeholder.
function symphonyIdentity(sym) {
  var name = sym && typeof sym.name === 'string' ? sym.name : '';
  var num = sym ? sym.github_project_number : null;
  var hasNum = typeof num === 'number' && isFinite(num) && num > 0;
  var org = sym && typeof sym.github_org === 'string' ? sym.github_org : '';
  var repo = sym && typeof sym.repo === 'string' ? sym.repo : '';
  var repoName = org && repo ? org + '/' + repo : repo;
  var project = hasNum ? 'GitHub Project #' + num : '';
  return {
    name: name,
    projectText: project,
    projectUrl: hasNum && org ? 'https://github.com/orgs/' + encodeURIComponent(org) + '/projects/' + num : '',
    repoText: repoName,
    repoUrl: repo && org ? 'https://github.com/' + encodeURIComponent(org) + '/' + encodeURIComponent(repo) : '',
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
    options += '<option value="' + esc(name) + '"' + (name === choice ? ' selected' : '') + '>' + esc(name) + '</option>';
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
  else renderDashboardExtras(scoped);
  afRender();
}
