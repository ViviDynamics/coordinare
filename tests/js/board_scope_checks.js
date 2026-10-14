// 16, 139: the Cards panel renders the snapshot the header choice leaves. A
// choice narrows the snapshot with symphonyScope(); these checks render the
// result and confirm no other symphony's cards leak through.
//
// Usage: node board_scope_checks.js <helpers.js> <performers.js+waiting.js> <symphonies.js>

const fs = require('fs');
const assert = require('assert');

const elements = Object.fromEntries(['swimlane-section', 'active-work-card'].map(id => [id, {
  innerHTML: '', style: {}, classList: {toggle() {}, remove() {}},
}]));
var document = {getElementById(id) {return elements[id] || null;}};
var _lastState = null;
var STALE_THRESHOLD_MS = 300000;
eval(fs.readFileSync(process.argv[2], 'utf8'));
eval(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));

const sessions = [
  {card_id: 'working', card_title: 'Other symphony active card', phase: 'blocked'},
  {card_id: 'completed', card_title: 'Completed retained card', phase: 'idle'},
  {card_id: 'parked', card_title: 'Backlog retained PR', phase: 'monitoring_pr'},
];
const knownBoard = {TODO: [], BLOCKED: ['working'], IN_PROGRESS: [], IN_REVIEW: [], BACKLOG: ['parked'], DONE: ['completed']};
const other = {name: 'B', state: {board_snapshot: knownBoard, board_titles: {working: 'Other symphony active card'}}};
const failures = [];
let passed = 0;
function check(name, run) {
  try {run(); passed++;} catch (error) {failures.push(name + ': ' + error.message);}
}

// Renders the panel for one header choice (null means the snapshot as it arrived).
function render(choice, symphonies, active = sessions) {
  const full = {phase: 'blocked', symphonies, active_sessions: active};
  const view = choice === null ? full : symphonyScope(full, choice);
  renderActiveWorkPanels(view);
  return elements['swimlane-section'].innerHTML;
}

check('an unavailable symphony cannot leak another known board', () => {
  const html = render('A', [{name: 'A', state: null}, other]);
  assert(!html.includes('data-card-id='));
  assert(!html.includes('Other symphony active card'));
});
check('a known empty symphony cannot leak other sessions', () => {
  const html = render('A', [{name: 'A', state: {board_snapshot: {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []}}}, other]);
  assert(!html.includes('data-card-id='));
});
check('Backlog and Done stay hidden in a known board', () => {
  const hidden = {name: 'B', state: {board_snapshot: {...knownBoard, BLOCKED: []}}};
  const html = render('B', [hidden]);
  assert(!html.includes('Completed retained card'));
  assert(!html.includes('Backlog retained PR'));
  assert(!html.includes('data-card-id='));
});
check('the chosen symphony shows its own cards only', () => {
  const html = render('B', [{name: 'A', state: null}, other]);
  assert(html.includes('data-card-id="working"'));
  assert(html.includes('Other symphony active card'));
  assert(!html.includes('data-card-id="completed"'));
  assert(!html.includes('data-card-id="parked"'));
});
check('All shows the cards of every symphony, each named', () => {
  const html = render('', [{name: 'A', state: null}, other]);
  assert(html.includes('data-card-id="working"'));
  assert(html.includes('swimlane-card-symphony">B<'));
});
check('a single symphony is not named on its cards', () => {
  const html = render('', [other]);
  assert(html.includes('data-card-id="working"'));
  assert(!html.includes('swimlane-card-symphony'));
});
check('legacy missing symphony boards keep the active-session fallback', () => {
  const html = render(null, [], [sessions[0]]);
  assert(html.includes('data-card-id="working"'));
});
check('a legacy null board keeps the fallback', () => {
  const html = render('A', [{name: 'A', state: {board_snapshot: null}}], [sessions[0]]);
  assert(html.includes('data-card-id="working"'));
});
check('a known empty snapshot is not legacy missing data', () => {
  assert(!render('A', [{name: 'A', state: {board_snapshot: {}}}]).includes('data-card-id='));
});

failures.forEach(failure => console.error('FAIL ' + failure));
console.log(`SUMMARY ${passed} ${failures.length}`);
process.exit(failures.length === 0 ? 0 : 1);
