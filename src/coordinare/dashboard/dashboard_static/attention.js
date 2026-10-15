// 153: the needs-attention signal. attentionItems() derives, from the snapshot
// the page already holds, what needs a person. The functions below it put that
// list in the tab title, on the favicon, on the nav and in the Dashboard banner.
// Everything above updateAttention() is pure and runs under node
// (tests/js/attention_checks.js); the rest touches the page and is only called
// from the state_update listener and the router.
var ATTENTION_KIND_LABELS = {
  question: 'Question',
  needs_human: 'Needs human',
  subsystem_down: 'Subsystem down'
};
var ATTENTION_DISMISS_KEY = 'coordinare.attentionDismissed';

// Only an http(s) URL from the snapshot may become a link.
function attentionExternalUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : '';
}

// The run detail for a stage, or '' when the card has no stage.
function attentionRunHref(stage) {
  return stage ? '/performers/' + encodeURIComponent(stage) : '';
}

function attentionCardLabel(number, title, fallback) {
  var head = number != null && number !== '' ? '#' + String(number) : '';
  var name = String(title || '');
  return (head + (head && name ? ' ' : '') + name) || fallback;
}

// The items that need a person, questions first, then blocked cards, then
// subsystems. Each is {kind, key, card_id, title, detail, href, since, stage}.
//  - question: one per open clarification question (open_questions). Its
//    since is asked_at.
//  - needs_human: a session in the blocked phase that has no open question of
//    its own, so a card is counted once. Its reason is the cause the conductor
//    named (142), in words; a stop with no known cause shows none.
//  - subsystem_down: a required subsystem reporting unavailable. Optional ones
//    are left out, so an integration nobody configured never holds the badge.
// Not derived: a PR waiting on review (monitoring_pr). The snapshot does not
// say whether that wait is for a human review or for CI, and the daemon keeps
// no per-session review-required flag, so counting it would invent a claim.
// 139: an item on a card no symphony claims is still shown under every symphony
// choice, and says so, so the operator knows whose it is.
// 142: true when the conductor holds this card for a dependency with blockers (the page's own
// skip record, top-level or in the card's symphony). The chip reads the same record.
function attentionDependencyWait(s, cardId) {
  if (!cardId) return false;
  var maps = [s.session_skip_reasons];
  (Array.isArray(s.symphonies) ? s.symphonies : []).forEach(function(sym) {
    maps.push(sym && sym.state ? sym.state.session_skip_reasons : null);
  });
  return maps.some(function(map) {
    if (!map || !Object.prototype.hasOwnProperty.call(map, cardId)) return false;
    var r = map[cardId];
    return !!r && r.reason === 'dependency_blocked' && Array.isArray(r.blockers) && r.blockers.length > 0;
  });
}

// 142: the words for a cause the conductor named, or '' when it named none.
function attentionCauseText(code) {
  var map = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY.block_cause_labels) || {};
  return Object.prototype.hasOwnProperty.call(map, code) ? String(map[code]) : '';
}

function attentionDetail(text, unclaimed) {
  if (!unclaimed) return text;
  var note = "not on any symphony's GitHub Project";
  return text ? text + ' · ' + note : note;
}

function attentionItems(s) {
  s = s || {};
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var questions = Array.isArray(s.open_questions) ? s.open_questions : [];
  var subsystems = Array.isArray(s.subsystems) ? s.subsystems : [];
  var items = [];
  var askedCards = {};
  questions.forEach(function(q) {
    var obj = typeof q === 'string' ? { text: q } : (q || {});
    var cardId = obj.card_id ? String(obj.card_id) : '';
    if (cardId) askedCards[cardId] = true;
    var stage = typeof obj.stage === 'string' ? obj.stage : '';
    items.push({
      kind: 'question',
        key: 'question:' + (cardId || 'card') + ':' + String(obj.text || ''),
      card_id: cardId || null,
      title: attentionCardLabel(obj.card_number, obj.card_title, 'Card'),
      detail: attentionDetail(String(obj.text || ''), obj.unclaimed),
      unclaimed: !!obj.unclaimed,
      href: attentionExternalUrl(obj.issue_url) || attentionRunHref(stage) || '/',
      since: obj.asked_at || null,
      stage: stage
    });
  });
  sessions.forEach(function(sess) {
    if (!sess || sess.phase !== 'blocked') return;
    var cardId = sess.card_id ? String(sess.card_id) : '';
    if (cardId && askedCards[cardId]) return;
    // 142: a card held for a dependency is Waiting on #N, the same kind its chip names, not Needs human.
    if (attentionDependencyWait(s, cardId)) return;
    var stage = typeof sess.performer_stage === 'string' ? sess.performer_stage : '';
    items.push({
      kind: 'needs_human',
      key: 'needs_human:' + (cardId || 'card'),
      card_id: cardId || null,
      title: attentionCardLabel(sess.issue_number, sess.card_title, 'Card'),
      detail: attentionDetail(attentionCauseText(sess.block_cause), sess.unclaimed),
      unclaimed: !!sess.unclaimed,
      href: attentionExternalUrl(sess.issue_url) || attentionRunHref(stage) || '/',
      since: null,
      stage: stage
    });
  });
  subsystems.forEach(function(sub) {
    if (!sub || !sub.required || sub.status !== 'unavailable') return;
    items.push({
      kind: 'subsystem_down',
      key: 'subsystem:' + String(sub.name || ''),
      card_id: null,
      title: String(sub.name || 'Subsystem'),
      detail: String(sub.details || ''),
      href: '#subsystems-section',
      since: sub.checked_at || null,
      stage: ''
    });
  });
  return items;
}

// "(2) Coordinare": the count goes in front of whatever the page title is, and
// nothing is added at zero.
function attentionTitle(base, count) {
  var n = Number(count) || 0;
  var text = String(base || '');
  return n > 0 ? '(' + n + ') ' + text : text;
}

function attentionCountText(n) {
  return n === 1 ? '1 needs attention' : String(n) + ' need attention';
}

// Identifies one set of items. The banner stays dismissed while this is
// unchanged, and comes back when an item is added, removed or re-asked. A
// subsystem's time is when it was last probed, which moves on every probe
// while nothing has changed, so it is left out.
function attentionSignature(items) {
  return (items || []).map(function(it) {
    var since = it.kind === 'subsystem_down' ? '' : (it.since || '');
    // 139: an item's "not on any symphony's GitHub Project" label comes and goes with the scope.
    return it.kind + '|' + it.key + '|' + since + (it.unclaimed ? '|unclaimed' : '');
  }).sort().join('\n');
}

// The Dashboard badge counts every item. The Performers badge counts the
// items that belong to a run.
function attentionNavCounts(items) {
  var list = items || [];
  return {
    dashboard: list.length,
    performers: list.filter(function(it) { return !!it.stage; }).length
  };
}

function attentionRowHtml(it) {
  var kind = ATTENTION_KIND_LABELS[it.kind] || String(it.kind || '');
  var href = String(it.href || '/');
  var attrs;
  if (/^https?:\/\//i.test(href)) {
    attrs = ' target="_blank" rel="noopener"';
  } else if (href.charAt(0) === '/') {
    attrs = ' onclick="navigate(event,this.pathname)"';
  } else {
    attrs = '';
  }
  var link = '<a href="' + esc(href) + '"' + attrs + '>' + esc(it.title) + '</a>';
  var detail = it.detail ? ': <span class="attention-detail">' + esc(it.detail) + '</span>' : '';
  var since = '';
  if (it.since) {
    var prefix = it.kind === 'subsystem_down' ? 'checked ' : 'asked ';
    since = ' <span class="muted">' + esc(prefix) + timeHtml(it.since, '') + '</span>';
  }
  return '<li class="attention-item" data-attention-key="' + esc(it.key) + '">' +
    '<span class="attention-kind">' + esc(kind) + '</span> ' + link + detail + since + '</li>';
}

function attentionListHtml(items) {
  return (items || []).map(attentionRowHtml).join('');
}

// ---------------------------------------------------------------------------
// Page wiring. Nothing above this line touches the page.
// ---------------------------------------------------------------------------

var _pageTitleBase = null;      // the page's own title, without the count
var _attentionCount = 0;
var _attentionSig = '';
var _attentionRenderedSig = null;
var _faviconOriginal = null;    // {href, type} of the icon as the page shipped it
var _faviconShown = 0;          // the count on the favicon now, 0 for the plain icon
var _faviconSeq = 0;
var ATTENTION_ICON_PX = 32;

// Every page title goes through here, so the count survives navigation.
function setPageTitle(base) {
  _pageTitleBase = String(base || '');
  document.title = attentionTitle(_pageTitleBase, _attentionCount);
}

function renderAttentionNav(counts) {
  var links = document.querySelectorAll('#navbar .nav-link[data-attention]');
  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var n = counts[link.getAttribute('data-attention')] || 0;
    if (!link.hasAttribute('data-base-label')) link.setAttribute('data-base-label', link.getAttribute('aria-label') || '');
    var base = link.getAttribute('data-base-label');
    link.setAttribute('aria-label', n > 0 ? base + ', ' + attentionCountText(n) : base);
    var badge = link.querySelector('.nav-badge');
    if (badge) {
      badge.textContent = n > 0 ? String(n) : '';
      badge.hidden = n === 0;
    }
  }
}

function restoreFavicon(link) {
  if (_faviconShown === 0 || !_faviconOriginal) return;
  link.setAttribute('href', _faviconOriginal.href);
  if (_faviconOriginal.type) link.setAttribute('type', _faviconOriginal.type);
  else link.removeAttribute('type');
  _faviconShown = 0;
}

// The icon is drawn onto a canvas with a count on it. Without canvas, or if the
// image does not load, the page keeps its plain icon and the title still counts.
function renderAttentionFavicon(count) {
  var link = document.querySelector('link[rel="icon"]');
  if (!link) return;
  if (_faviconOriginal === null) {
    _faviconOriginal = { href: link.getAttribute('href') || '', type: link.getAttribute('type') };
  }
  var seq = ++_faviconSeq;
  if (count <= 0) { restoreFavicon(link); return; }
  if (count === _faviconShown) return;
  try {
    var canvas = document.createElement('canvas');
    canvas.width = ATTENTION_ICON_PX;
    canvas.height = ATTENTION_ICON_PX;
    var ctx = canvas.getContext ? canvas.getContext('2d') : null;
    if (!ctx) return;
    var img = new Image();
    img.onload = function() {
      if (seq !== _faviconSeq) return;
      try {
        var px = ATTENTION_ICON_PX;
        var r = px * 0.32;
        var cx = px - r;
        var cy = r;
        ctx.clearRect(0, 0, px, px);
        ctx.drawImage(img, 0, 0, px, px);
        ctx.fillStyle = '#b02a37';
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#fffefb';
        ctx.font = 'bold ' + Math.round(r * 1.5) + 'px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(count > 9 ? '9+' : String(count), cx, cy + 1);
        link.setAttribute('href', canvas.toDataURL('image/png'));
        link.setAttribute('type', 'image/png');
        _faviconShown = count;
      } catch (_noCanvas) { /* keep the plain icon */ }
    };
    img.onerror = function() { /* keep the plain icon */ };
    img.src = _faviconOriginal.href;
  } catch (_noImage) { /* keep the plain icon */ }
}

function attentionDismissedSignature() {
  try { return window.sessionStorage.getItem(ATTENTION_DISMISS_KEY) || ''; } catch (_noStorage) { return ''; }
}

function storeAttentionDismissed(sig) {
  try {
    if (sig) window.sessionStorage.setItem(ATTENTION_DISMISS_KEY, sig);
    else window.sessionStorage.removeItem(ATTENTION_DISMISS_KEY);
  } catch (_noStorage) { /* dismissal lasts this page view only */ }
}

function renderAttentionBanner(items, sig) {
  var panel = document.getElementById('attention-panel');
  if (!panel) return;
  // Nothing left to show: forget the dismissal, so the next item shows again.
  // 139: and drop the rows, so a list hidden here never keeps what the scope removed.
  if (items.length === 0) {
    storeAttentionDismissed('');
    var emptied = document.getElementById('attention-list');
    if (emptied) emptied.innerHTML = '';
    _attentionRenderedSig = null;
  }
  var show = items.length > 0 && sig !== attentionDismissedSignature();
  panel.hidden = !show;
  if (!show || sig === _attentionRenderedSig) return;
  // Rewritten only when the set changes, so the live region announces new items
  // and not every state update.
  document.getElementById('attention-count').textContent = attentionCountText(items.length);
  document.getElementById('attention-list').innerHTML = attentionListHtml(items);
  _attentionRenderedSig = sig;
}

// 154 review: the banner is one line until it is opened. The list shows only when expanded.
function toggleAttentionList() {
  var panel = document.getElementById('attention-panel');
  var button = document.getElementById('attention-toggle');
  if (!panel || !button) return;
  var open = !panel.classList.contains('expanded');
  panel.classList.toggle('expanded', open);
  button.setAttribute('aria-expanded', String(open));
  button.textContent = open ? 'Hide' : 'Show';
}

// The banner's dismiss button. The title and the favicon keep the true count.
function dismissAttention() {
  if (!_attentionSig) return;
  storeAttentionDismissed(_attentionSig);
  var panel = document.getElementById('attention-panel');
  if (panel) panel.hidden = true;
  var main = document.getElementById('main-content');
  if (main) main.focus();
}

function updateAttention(s) {
  var items = attentionItems(s);
  _attentionCount = items.length;
  _attentionSig = attentionSignature(items);
  if (_pageTitleBase === null) _pageTitleBase = document.title;
  document.title = attentionTitle(_pageTitleBase, _attentionCount);
  renderAttentionNav(attentionNavCounts(items));
  renderAttentionFavicon(_attentionCount);
  renderAttentionBanner(items, _attentionSig);
}
