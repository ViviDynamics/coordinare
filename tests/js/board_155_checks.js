// 155: checks for the board's pure functions: the PR checks icon, the Done (last 24h)
// lane, the inactive-symphony classification and the switcher's label for it. The
// functions run as shipped. The bundle is helpers.js, performers.js, waiting.js,
// timeline.js and symphonies.js, concatenated by tests/unit/test_155_board_js.py.
//
// Usage: node board_155_checks.js <bundle.js>

const fs = require('fs');
const assert = require('assert');

const elements = {};
var document = {getElementById(id) {return elements[id] || null;}};
var _lastState = null;
var STALE_THRESHOLD_MS = 300000;
eval(fs.readFileSync(process.argv[2], 'utf8'));

const NOW = Date.parse('2026-10-14T12:00:00+00:00');
const HOUR = 3600000;
const failures = [];
let passed = 0;
function check(name, run) {
  try {run(); passed++;} catch (error) {failures.push(name + ': ' + error.message);}
}
function iso(msBefore) {
  return new Date(NOW - msBefore).toISOString().replace('.000Z', '+00:00');
}

// --- the PR checks icon ---------------------------------------------------------- //

check('a pass, fail or running state gets its own class and title', () => {
  assert.ok(checksIconHtml('pass').indexOf('checks-pass') !== -1);
  assert.ok(checksIconHtml('pass').indexOf('title="PR checks: passing"') !== -1);
  assert.ok(checksIconHtml('fail').indexOf('checks-fail') !== -1);
  assert.ok(checksIconHtml('fail').indexOf('title="PR checks: failing"') !== -1);
  assert.ok(checksIconHtml('running').indexOf('checks-running') !== -1);
  assert.ok(checksIconHtml('running').indexOf('title="PR checks: running"') !== -1);
});

check('no state, null and an unnamed state all show the unknown icon with a title', () => {
  for (const state of [null, undefined, '', 'bogus', 'constructor']) {
    const html = checksIconHtml(state);
    assert.ok(html.indexOf('checks-unknown') !== -1, 'unknown class for ' + state);
    assert.ok(html.indexOf('title="PR checks: unknown') !== -1, 'unknown title for ' + state);
  }
});

check('a state from the snapshot is escaped, never written as markup', () => {
  const html = checksIconHtml('<img src=x onerror=alert(1)>');
  assert.ok(html.indexOf('<img') === -1);
  assert.ok(html.indexOf('checks-unknown') !== -1);
});

// --- the Done (last 24h) lane ---------------------------------------------------- //

function doneSnapshot(strips, titles) {
  return {
    symphonies: [{name: 'alpha', state: {
      board_snapshot: {TODO: [], DONE: ['new', 'old', 'bare', 'frac', 'other'], IN_REVIEW: ['live']},
      board_titles: titles || {new: 'Newest merge', old: 'Two days ago', bare: 'Never merged here', frac: 'Fractional', other: 'Other'},
    }}],
    card_pipeline: {alpha: strips},
  };
}

const STRIPS = {
  new: {current: null, statuses: [], checks: 'pass', merged_at: iso(HOUR)},
  old: {current: null, statuses: [], checks: 'pass', merged_at: iso(48 * HOUR)},
  bare: {current: null, statuses: [], checks: null, merged_at: null},
  frac: {current: null, statuses: [], checks: null, merged_at: '2026-10-13T13:00:00.123456+00:00'},
  other: {current: null, statuses: [], checks: null, merged_at: iso(2 * HOUR)},
};

check('the lane counts only the Done cards merged within the last 24 hours, newest first', () => {
  const entries = doneLaneEntries(doneSnapshot(STRIPS), NOW);
  assert.deepStrictEqual(entries.map(e => e.iid), ['new', 'other', 'frac']);
});

check('a card with no recorded merge is not counted, and a fraction of a second is read', () => {
  const entries = doneLaneEntries(doneSnapshot(STRIPS), NOW);
  assert.ok(entries.every(e => e.iid !== 'bare'));
  const frac = entries.find(e => e.iid === 'frac');
  assert.ok(frac, 'fractional timestamp counted');
  assert.strictEqual(frac.label, 'Fractional');
});

check('the window is 24 hours, so a card merged 25 hours ago is out', () => {
  const strips = {new: {merged_at: iso(25 * HOUR)}};
  const entries = doneLaneEntries({symphonies: [{name: 'alpha', state: {board_snapshot: {DONE: ['new']}}}],
    card_pipeline: {alpha: strips}}, NOW);
  assert.strictEqual(entries.length, 0);
});

check('a Done card with no strip, and no snapshot at all, gives an empty lane', () => {
  assert.deepStrictEqual(doneLaneEntries(null, NOW), []);
  assert.deepStrictEqual(doneLaneEntries({symphonies: [{name: 'alpha', state: {board_snapshot: {DONE: ['x']}}}]}, NOW), []);
});

check('a merge in another symphony does not count under this one', () => {
  const snap = {
    symphonies: [{name: 'alpha', state: {board_snapshot: {DONE: ['x']}}}],
    card_pipeline: {beta: {x: {merged_at: iso(HOUR)}}},
  };
  assert.strictEqual(doneLaneEntries(snap, NOW).length, 0);
});

check('the lane is collapsed by default and its summary carries the count', () => {
  const html = doneLaneHtml(doneLaneEntries(doneSnapshot(STRIPS), NOW));
  assert.ok(html.indexOf('<details class="done-lane"') === 0, 'a details element');
  assert.ok(html.indexOf(' open') === -1, 'not open');
  assert.ok(html.indexOf('<summary>Done (last 24h) <span class="swimlane-col-count">3</span></summary>') !== -1);
});

check('the lane says what the count covers, and an empty lane says so', () => {
  const html = doneLaneHtml([]);
  assert.ok(html.indexOf('Counts merges coordinare recorded in the last 24 hours.') !== -1);
  assert.ok(html.indexOf('No merges in the last 24 hours.') !== -1);
  assert.ok(html.indexOf('<span class="swimlane-col-count">0</span>') !== -1);
});

check('a card title in the lane is escaped', () => {
  const entries = [{iid: 'x', label: '<script>alert(1)</script>', mergedAt: iso(HOUR), at: NOW - HOUR}];
  const html = doneLaneHtml(entries);
  assert.ok(html.indexOf('<script>') === -1);
  assert.ok(html.indexOf('&lt;script&gt;') !== -1);
});

// --- the inactive symphony ------------------------------------------------------- //

const PLACEHOLDER = {name: 'placeholder', cycle_count: 0, error_count: 0, last_poll_at: null,
  state: {board_snapshot: {TODO: [], DONE: [], IN_REVIEW: [], IN_PROGRESS: [], BLOCKED: [], BACKLOG: []}}};

check('a symphony with no cards and no poll is inactive', () => {
  assert.strictEqual(symphonyIsInactive(PLACEHOLDER), true);
  assert.strictEqual(symphonyIsInactive(Object.assign({}, PLACEHOLDER, {state: null})), true);
});

check('a symphony that has been polled is active, even with no cards', () => {
  assert.strictEqual(symphonyIsInactive(Object.assign({}, PLACEHOLDER, {last_poll_at: iso(HOUR)})), false);
  assert.strictEqual(symphonyIsInactive(Object.assign({}, PLACEHOLDER, {cycle_count: 4})), false);
});

check('a symphony with a card on its board is active, even if never polled', () => {
  const sym = Object.assign({}, PLACEHOLDER, {state: {board_snapshot: {TODO: ['c1']}}});
  assert.strictEqual(symphonyIsInactive(sym), false);
});

check('a missing symphony is not inactive, and neither is one whose poll fields are absent', () => {
  assert.strictEqual(symphonyIsInactive(null), false);
  assert.strictEqual(symphonyIsInactive(undefined), false);
  assert.strictEqual(symphonyIsInactive({name: 'bare'}), false);
});

check('the switcher labels an inactive symphony and leaves the others alone', () => {
  const s = {symphonies: [PLACEHOLDER, {name: 'live', cycle_count: 2, last_poll_at: iso(HOUR), state: {board_snapshot: {TODO: []}}}]};
  const html = symphonySwitcherHtml(s, SYMPHONY_ALL);
  assert.ok(html.indexOf('>placeholder (Never checked: check config)</option>') !== -1, html);
  assert.ok(html.indexOf('>live</option>') !== -1, html);
});

check('the Cards panel says a never-checked symphony was never checked', () => {
  elements['swimlane-section'] = {innerHTML: '', style: {}, classList: {toggle() {}, remove() {}}};
  renderActiveWorkPanels({phase: 'idle', symphonies: [PLACEHOLDER], active_sessions: []});
  assert.ok(elements['swimlane-section'].innerHTML.indexOf('Never checked: check config') !== -1);
});

// --- the snapshot's timestamps -------------------------------------------------- //

check('a microsecond timestamp reads as the same instant as its whole-second form', () => {
  assert.strictEqual(isoMs('2026-10-13T13:00:00.123456+00:00'), Date.parse('2026-10-13T13:00:00+00:00'));
});

if (failures.length > 0) {
  failures.forEach(f => console.log('FAIL ' + f));
}
console.log('SUMMARY ' + passed + ' ' + failures.length);
process.exit(failures.length === 0 ? 0 : 1);
