// 155: checks for the board's pure functions: the PR checks icon, the Done (last 24h)
// lane, the unchecked-symphony notes and the timestamps they read. The functions run as
// shipped. The bundle is helpers.js, performers.js, waiting.js, timeline.js and
// symphonies.js, concatenated by tests/unit/test_155_board_js.py.
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
const HEAD_A = 'a'.repeat(12);
const HEAD_B = 'b'.repeat(12);

// --- the PR checks icon: gate-time state, stale, environment, unknown ----------------- //

function checks(state, extra) {
  return Object.assign({state: state, head: HEAD_A, at: iso(12 * 60000), stale: false, current_checked: true}, extra || {});
}

check('a pass, fail or running state gets its own class and a hand-off title with the age', () => {
  const pass = checksIconHtml(checks('pass'), NOW);
  assert.ok(pass.indexOf('checks-pass') !== -1, 'pass class');
  assert.ok(pass.indexOf('title="PR checks at review hand-off: passing (12m 0s ago)"') !== -1, pass);
  assert.ok(checksIconHtml(checks('fail'), NOW).indexOf('PR checks at review hand-off: failing (12m 0s ago)') !== -1);
  assert.ok(checksIconHtml(checks('running'), NOW).indexOf('checks-running') !== -1);
});

check('a current result has no stale wording', () => {
  assert.ok(checksIconHtml(checks('fail'), NOW).indexOf('earlier commit') === -1);
  assert.ok(checksIconHtml(checks('fail'), NOW).indexOf('checks-stale') === -1);
});

check('a stale result is neutral and says it is as of an earlier commit, still under the hand-off title', () => {
  const html = checksIconHtml(checks('fail', {stale: true}), NOW);
  assert.ok(html.indexOf('checks-stale') !== -1, 'neutral class');
  assert.ok(html.indexOf('PR checks at review hand-off: failing') !== -1, 'hand-off title kept');
  assert.ok(html.indexOf('checks as of an earlier commit') !== -1, 'stale wording');
});

check('an environment block is its own neutral state, not failing', () => {
  const html = checksIconHtml(checks('blocked'), NOW);
  assert.ok(html.indexOf('checks-blocked') !== -1);
  assert.ok(html.indexOf('blocked by environment') !== -1);
  assert.ok(html.indexOf('checks-fail') === -1 && html.indexOf('failing') === -1);
});

check('a recorded unknown result shows the unknown icon with the age, under the hand-off title', () => {
  const html = checksIconHtml(checks('unknown'), NOW);
  assert.ok(html.indexOf('checks-unknown') !== -1);
  assert.ok(html.indexOf('PR checks at review hand-off: unknown (12m 0s ago)') !== -1, html);
});

check('a gate result with no current commit checked is neutral, never green or red', () => {
  for (const state of ['pass', 'fail', 'running']) {
    const html = checksIconHtml(checks(state, {current_checked: false}), NOW);
    assert.ok(html.indexOf('checks-unchecked') !== -1, 'neutral class for ' + state);
    assert.ok(html.indexOf('checks-pass') === -1 && html.indexOf('checks-fail') === -1 && html.indexOf('checks-running') === -1, html);
    assert.ok(html.indexOf('last gate at review hand-off: ' + ({pass: 'passing', fail: 'failing', running: 'running'})[state] +
      '; current commit not checked') !== -1, 'title for ' + state + ': ' + html);
  }
});

check('an unchecked head that is also stale still says the current commit was not checked', () => {
  const html = checksIconHtml(checks('fail', {current_checked: false, stale: false}), NOW);
  assert.ok(html.indexOf('current commit not checked') !== -1, html);
  assert.ok(html.indexOf('PR checks at review hand-off') === -1, 'no current-tense hand-off title');
});

check('no gate at all shows the unknown icon with its own title', () => {
  for (const value of [null, undefined, 'bogus']) {
    const html = checksIconHtml(value, NOW);
    assert.ok(html.indexOf('checks-unknown') !== -1);
    assert.ok(html.indexOf('PR checks: unknown, no CI gate has judged this card yet') !== -1, html);
  }
});

check('a state from the snapshot is escaped, never written as markup', () => {
  const html = checksIconHtml(checks('<img src=x onerror=alert(1)>'), NOW);
  assert.ok(html.indexOf('<img') === -1);
  assert.ok(html.indexOf('checks-unknown') !== -1);
});

// --- timestamps: microseconds and the server clock ------------------------------------ //

check('a microsecond timestamp reads as the same instant as its whole-second form', () => {
  assert.strictEqual(isoMs('2026-10-13T13:00:00.123456+00:00'), Date.parse('2026-10-13T13:00:00+00:00'));
});

check('the age of a microsecond timestamp is read, not NaN, and is the age of its whole second', () => {
  // The fraction is dropped before the parse, so the age is the whole-second one (2h 0m), never NaN.
  assert.strictEqual(fmtAgeAt('2026-10-14T10:00:00.123456+00:00', NOW), '2h 0m');
  assert.strictEqual(fmtAgeAt('2026-10-14T10:00:00+00:00', NOW), '2h 0m');
});

check('a malformed timestamp has no age rather than a wrong one', () => {
  assert.strictEqual(fmtAgeAt('not a time', NOW), null);
  assert.strictEqual(fmtAgeAt(null, NOW), null);
});

// --- the Done (last 24h) lane ---------------------------------------------------------- //

function doneSnapshot(strips, titles, extra) {
  return Object.assign({
    symphonies: [{name: 'alpha', state: {
      board_snapshot: {TODO: [], DONE: ['new', 'old', 'bare', 'frac', 'other'], IN_REVIEW: ['live']},
      board_titles: titles || {new: 'Newest merge', old: 'Two days ago', bare: 'Never merged here', frac: 'Fractional', other: 'Other'},
    }}],
    card_pipeline: {alpha: strips},
  }, extra || {});
}

const STRIPS = {
  new: {current: null, statuses: [], checks: null, merged_at: iso(HOUR)},
  old: {current: null, statuses: [], checks: null, merged_at: iso(48 * HOUR)},
  bare: {current: null, statuses: [], checks: null, merged_at: null},
  frac: {current: null, statuses: [], checks: null, merged_at: '2026-10-13T13:00:00.123456+00:00'},
  other: {current: null, statuses: [], checks: null, merged_at: iso(2 * HOUR)},
};

check('the lane counts the Done cards merged within the last 24 hours, newest first', () => {
  assert.deepStrictEqual(doneLaneEntries(doneSnapshot(STRIPS), NOW).map(e => e.iid), ['new', 'other', 'frac']);
});

check('a card with no recorded merge is not counted, and a fraction of a second is read', () => {
  const entries = doneLaneEntries(doneSnapshot(STRIPS), NOW);
  assert.ok(entries.every(e => e.iid !== 'bare'));
  assert.strictEqual(entries.find(e => e.iid === 'frac').label, 'Fractional');
});

check('the cut is the server snapshot time, not the browser clock', () => {
  // The snapshot was taken 30 hours ago. The merge is 31 hours before the browser's now, but only
  // 1 hour before the snapshot, so it is counted.
  const snapshotAt = iso(30 * HOUR);
  const snap = {symphonies: [{name: 'alpha', state: {board_snapshot: {DONE: ['x']}, board_titles: {x: 'Late'}}}],
    card_pipeline: {alpha: {x: {merged_at: iso(31 * HOUR)}}}, snapshot_at: snapshotAt};
  assert.deepStrictEqual(doneLaneEntries(snap, Date.parse(snapshotAt)).map(e => e.iid), ['x']);
  assert.strictEqual(doneLaneEntries(snap, NOW).length, 0, 'the browser clock would drop it');
});

check('the server snapshot time is used when present, and the browser clock when not', () => {
  assert.strictEqual(serverNowMs({snapshot_at: iso(HOUR)}), NOW - HOUR);
  const before = Date.now();
  const fallback = serverNowMs({});
  assert.ok(fallback >= before && fallback <= Date.now() + 1000);
});

check('a card in another symphony does not count under this one', () => {
  const snap = {symphonies: [{name: 'alpha', state: {board_snapshot: {DONE: ['x']}}}],
    card_pipeline: {beta: {x: {merged_at: iso(HOUR)}}}};
  assert.strictEqual(doneLaneEntries(snap, NOW).length, 0);
});

check('the lane is collapsed by default and its summary carries the count', () => {
  const html = doneLaneHtml(doneLaneEntries(doneSnapshot(STRIPS), NOW), NOW);
  assert.ok(html.indexOf('<details class="done-lane"') === 0);
  assert.ok(html.indexOf(' open') === -1);
  assert.ok(html.indexOf('<summary>Done (last 24h) <span class="swimlane-col-count">3</span></summary>') !== -1);
  assert.ok(html.indexOf('merged 1h 0m ago') !== -1, 'the age is read from the server clock');
});

check('the lane says what the count covers, and an empty lane says so', () => {
  const html = doneLaneHtml([], NOW);
  assert.ok(html.indexOf('Counts merges coordinare recorded in the last 24 hours.') !== -1);
  assert.ok(html.indexOf('No merges in the last 24 hours.') !== -1);
});

check('a card title in the lane is escaped', () => {
  const html = doneLaneHtml([{iid: 'x', label: '<script>alert(1)</script>', mergedAt: iso(HOUR), at: NOW - HOUR}], NOW);
  assert.ok(html.indexOf('<script>') === -1);
  assert.ok(html.indexOf('&lt;script&gt;') !== -1);
});

// --- the unchecked symphony: why it has no check yet --------------------------------- //

function symphony(extra) {
  return Object.assign({name: 'sym', cycle_count: 0, error_count: 0, last_poll_at: null,
    poll_interval_seconds: 30, daemon_cycles_completed: 0, state: null}, extra || {});
}

check('a symphony whose first cycle threw says the check failed, with the redacted error', () => {
  const note = symphonyUncheckedNote(symphony({error_count: 3, state: {last_error: 'GitHub 401 Bad credentials',
    board_snapshot: null}, daemon_cycles_completed: 1}));
  assert.strictEqual(note.kind, 'failed');
  assert.strictEqual(note.text, 'Last check failed: GitHub 401 Bad credentials');
  assert.strictEqual(symphonyIsInactive(symphony({error_count: 3, state: {last_error: 'x'}, daemon_cycles_completed: 1})), false,
    'a failed check is shown, not greyed as inactive');
});

check('a fresh boot before the first cycle reaches the symphony says not checked yet', () => {
  const note = symphonyUncheckedNote(symphony({daemon_cycles_completed: 0, state: null}));
  assert.strictEqual(note.kind, 'startup');
  assert.strictEqual(note.text, 'Not checked yet');
});

check('a webhook-only daemon with no webhook since boot says it is waiting for one', () => {
  const note = symphonyUncheckedNote(symphony({poll_interval_seconds: null, daemon_cycles_completed: 0,
    state: {board_snapshot: null}}));
  assert.strictEqual(note.kind, 'webhook');
  assert.strictEqual(note.text, 'Waiting for a webhook');
  assert.strictEqual(symphonyIsInactive(symphony({poll_interval_seconds: null})), false, 'waiting is not greyed');
});

check('a polled daemon that finished a cycle and never reached this symphony says never checked', () => {
  const sym = symphony({daemon_cycles_completed: 3});
  assert.strictEqual(symphonyUncheckedNote(sym).text, 'Never checked: check config');
  assert.strictEqual(symphonyIsInactive(sym), true);
});

check('a symphony that has been checked has no note, and inactive needs zero cards', () => {
  assert.strictEqual(symphonyUncheckedNote(symphony({cycle_count: 2, last_poll_at: iso(HOUR)})), null);
  assert.strictEqual(symphonyIsInactive(symphony({daemon_cycles_completed: 3,
    state: {board_snapshot: {TODO: ['c1']}}})), false);
});

check('a missing symphony or one with absent fields has no note', () => {
  assert.strictEqual(symphonyUncheckedNote(null), null);
  assert.strictEqual(symphonyUncheckedNote({name: 'bare'}), null);
  assert.strictEqual(symphonyIsInactive({name: 'bare'}), false);
});

check('the switcher labels each unchecked symphony with its own note, and a failed one with its error', () => {
  const s = {symphonies: [
    symphony({name: 'placeholder', daemon_cycles_completed: 3}),
    symphony({name: 'boom', error_count: 3, state: {last_error: 'GitHub 401 Bad credentials'}, daemon_cycles_completed: 1}),
    symphony({name: 'boot', daemon_cycles_completed: 0}),
    {name: 'live', cycle_count: 2, last_poll_at: iso(HOUR), state: {board_snapshot: {TODO: []}}},
  ]};
  const html = symphonySwitcherHtml(s, SYMPHONY_ALL);
  assert.ok(html.indexOf('>placeholder (Never checked: check config)</option>') !== -1, html);
  assert.ok(html.indexOf('>boom (Last check failed: GitHub 401 Bad credentials)</option>') !== -1, html);
  assert.ok(html.indexOf('>boot (Not checked yet)</option>') !== -1, html);
  assert.ok(html.indexOf('>live</option>') !== -1, html);
});

check('the Cards panel names the error, not never-checked, for a failed symphony', () => {
  elements['swimlane-section'] = {innerHTML: '', style: {}, classList: {toggle() {}, remove() {}}};
  renderActiveWorkPanels({phase: 'idle', symphonies: [symphony({error_count: 3, state: {last_error: 'GitHub 401 Bad credentials'}, daemon_cycles_completed: 1})],
    active_sessions: []});
  const html = elements['swimlane-section'].innerHTML;
  assert.ok(html.indexOf('Last check failed: GitHub 401 Bad credentials') !== -1, html);
  assert.ok(html.indexOf('Never checked') === -1, html);
});

check('the Cards panel says not checked yet at startup, and waiting for a webhook when webhook-only', () => {
  elements['swimlane-section'] = {innerHTML: '', style: {}, classList: {toggle() {}, remove() {}}};
  renderActiveWorkPanels({phase: 'idle', symphonies: [symphony({daemon_cycles_completed: 0})], active_sessions: []});
  assert.ok(elements['swimlane-section'].innerHTML.indexOf('Not checked yet') !== -1);
  renderActiveWorkPanels({phase: 'idle', symphonies: [symphony({poll_interval_seconds: null})], active_sessions: []});
  assert.ok(elements['swimlane-section'].innerHTML.indexOf('Waiting for a webhook') !== -1);
});

if (failures.length > 0) {
  failures.forEach(f => console.log('FAIL ' + f));
}
console.log('SUMMARY ' + passed + ' ' + failures.length);
process.exit(failures.length === 0 ? 0 : 1);
