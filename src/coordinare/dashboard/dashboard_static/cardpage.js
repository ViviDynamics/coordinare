// 233: the card page, at /cards/<symphony>/<card>. It reads only the timeline endpoint, so a
// finished card opens with no live session. The tracker, the stage runs and the lifecycle list
// are timeline.js's own rendering; this file addresses the page and says why it is empty.
// Loaded before app.js. The pure helpers come first and are checked under node
// (tests/js/card_page_233_checks.js). Every value shown goes through esc().

var CARD_PAGE_SECTION = 'card-page-section';
// The scope note the page last drew, so a snapshot redraws the page only when the scope changes.
var _cardPageNote = null;

// Whether a path is a card page address. The segments are checked by cardAddressFromPath.
function isCardPath(path) {
  return typeof path === 'string' && path.indexOf('/cards/') === 0;
}

// The address a /cards path names: {symphony, card}, or {invalid: true}. A segment that does not
// decode, or is not a plausible name by the run routes' pattern (RUN_NAME_PATTERN), is invalid.
// The server checks the address again (routers/pages.py), so this only decides what to show.
function cardAddressFromPath(path) {
  if (!isCardPath(path)) return { invalid: true };
  var parts = path.slice('/cards/'.length).replace(/\/+$/, '').split('/');
  if (parts.length !== 2) return { invalid: true };
  var decoded = [];
  for (var i = 0; i < parts.length; i++) {
    var value;
    try { value = decodeURIComponent(parts[i]); } catch (_malformed) { return { invalid: true }; }
    if (!RUN_NAME_PATTERN.test(value)) return { invalid: true };
    decoded.push(value);
  }
  return { symphony: decoded[0], card: decoded[1] };
}

// The path of a card page. Both segments are encoded, as the run routes encode theirs.
function cardPathFor(symphony, card) {
  return '/cards/' + encodeURIComponent(symphony) + '/' + encodeURIComponent(card);
}

// Whether a symphony and a card name a page the viewer can open. Anything else gets no link.
function cardPageAddressOk(symphony, card) {
  return RUN_NAME_PATTERN.test(String(symphony || '')) && RUN_NAME_PATTERN.test(String(card || ''));
}

// The note a card page shows when the header's symphony choice is another symphony, or ''.
// It matches the other views: under a choice, a card of another symphony is not shown, and
// nothing about it is fetched while the note stands.
function cardPageScopeNote(choice, symphony) {
  if (!choice || !symphony || choice === symphony) return '';
  return 'This card belongs to ' + symphony + '. The header shows ' + choice +
    ', so it is not shown here. Choose All symphonies or ' + symphony + ' in the header to see it.';
}

// The "Full timeline" link on the live card detail. Empty when the address is not one the page can name.
function cardFullTimelineHtml(symphony, card) {
  if (!cardPageAddressOk(symphony, card)) return '';
  return '<div class="card-full-timeline"><a href="' + esc(cardPathFor(symphony, card)) +
    '" onclick="navigate(event,this.pathname)">Full timeline</a></div>';
}

// The header's symphony choice for the page now. It reads the scoped view, never the raw snapshot.
function cardPageChoice() {
  return typeof currentSymphonyChoice === 'function' ? currentSymphonyChoice(currentScopedState()) : '';
}

// Writes the card page for an address: its heading, then the scope note or the timeline.
function cardPageRoute(path) {
  var section = document.getElementById(CARD_PAGE_SECTION);
  if (!section) return;
  var addr = cardAddressFromPath(path);
  if (addr.invalid) {
    _cardPageNote = null;
    section.innerHTML = '<div class="muted">This is not a card address.</div>';
    return;
  }
  var note = cardPageScopeNote(cardPageChoice(), addr.symphony);
  _cardPageNote = note;
  var heading = '<div class="card-page-title">' + esc(addr.card) + '</div>' +
    '<div class="muted">' + esc(addr.symphony) + '</div>';
  if (note) {
    section.innerHTML = heading + '<div class="muted card-page-scope">' + esc(note) + '</div>';
    return;
  }
  section.innerHTML = heading + timelinePlaceholderHtml(addr.symphony, addr.card);
  timelineHydrate(section);
}

// On each snapshot: the page is redrawn only when the scope it shows has changed.
function cardPageRefresh() {
  if (!isCardPath(location.pathname)) return;
  var addr = cardAddressFromPath(location.pathname);
  var note = addr.invalid ? null : cardPageScopeNote(cardPageChoice(), addr.symphony);
  if (note !== _cardPageNote) cardPageRoute(location.pathname);
}
