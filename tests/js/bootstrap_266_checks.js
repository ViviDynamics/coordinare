// 266: checks for the env bootstrap's browser words: the Symphonies cell and its history, the scope of
// each symphony's bootstrap, the status strip's item, the held card's reason and the feed's labels.
// The functions run as shipped. The bundle is helpers.js, usage.js, symphonies.js, layout.js, waiting.js
// and activity_streams.js, concatenated by tests/unit/test_266_bootstrap_js.py.
//
// Usage: node bootstrap_266_checks.js <bundle.js>

const fs = require('fs');
const assert = require('assert');

const elements = {};
var document = {getElementById(id) {return elements[id] || null;}};
var window = {};
var _lastState = null;
eval(fs.readFileSync(process.argv[2], 'utf8'));

const failures = [];
let passed = 0;
function check(name, run) {
  try {run(); passed++;} catch (error) {failures.push(name + ': ' + error.message);}
}
const EM = String.fromCharCode(0x2014);
const EN = String.fromCharCode(0x2013);
function noDashes(text) {
  assert.ok(String(text).indexOf(EM) === -1 && String(text).indexOf(EN) === -1, 'a dash in: ' + text);
}

const GEMFILE = 'verify.sh non-zero: Could not locate Gemfile';
function boot(extra) {
  return Object.assign({state: 'not_started', label: 'Not started', attempt: null, max_attempts: 3,
    commit: null, started_at: null, elapsed_seconds: null, next_attempt_at: null, ready_since: null,
    reason: null, reason_tail: [], reset_hint: null, backend: null, model: null, performer_id: null,
    pod: null, job_id: null, infra_failures: 0, history: []}, extra || {});
}
function sym(name, bootExtra, extra) {
  return Object.assign({name: name, github_project_number: 16, github_org: 'acme', repo: name + '-repo',
    priority: 0, cycle_count: 2, error_count: 0, last_poll_at: '2026-10-14T11:00:00+00:00',
    poll_interval_seconds: 30, daemon_cycles_completed: 2, state: null, enabled: true,
    env_bootstrap_performer_id: 'perf-1', cards_active: 0, cards_queued: 0, cards_paused: 0,
    env_status: {state: 'bootstrapping', reason: 'An env bootstrap is running.'},
    env_bootstrap: boot(bootExtra),
    spend_week: {tracked: false, reason: 'Spend is not recorded.', tokens: null, cost: null}}, extra || {});
}

// --- the Symphonies cell ----------------------------------------------------------------------------- //
check('the Env word is the bootstrap state, and its reason is the bootstrap label', () => {
  const env = symphonyEnvView(sym('a', {state: 'bootstrapping', label: 'Bootstrapping, run 2 of 3'}));
  assert.strictEqual(env.label, 'Bootstrapping');
  assert.strictEqual(env.reason, 'Bootstrapping, run 2 of 3');
});
check('a symphony without a bootstrap view keeps the env_status words', () => {
  const env = symphonyEnvView({env_status: {state: 'ready', reason: 'The env cache is ready for this symphony\'s performers.'}});
  assert.strictEqual(env.label, 'Ready');
});
check('a running bootstrap says when it started, how long it has run, and which backend and pod', () => {
  const html = symphonyBootstrapHtml(boot({state: 'bootstrapping', label: 'Bootstrapping, run 2 of 3',
    started_at: '2026-10-15T11:00:00+00:00', elapsed_seconds: 90, backend: 'claude_code', model: 'sonnet',
    pod: 'pod-abc', attempt: 2}));
  assert.ok(html.indexOf('elapsed 1m') !== -1, html);
  assert.ok(html.indexOf('claude_code sonnet') !== -1, html);
  assert.ok(html.indexOf('Pod pod-abc') !== -1, html);
  assert.ok(html.indexOf('<time') !== -1, 'the start is a time element');
});
check('a failed bootstrap shows its reason, the verify tail in a closed disclosure and no open attribute', () => {
  const html = symphonyBootstrapHtml(boot({state: 'failed', label: 'Failed on run 1 of 3', reason: GEMFILE,
    reason_tail: ['rbenv: ruby: command not found', GEMFILE]}));
  assert.ok(html.indexOf(GEMFILE) !== -1, html);
  assert.ok(html.indexOf('<details class="sym-boot-tail"><summary>Verify output</summary>') !== -1, html);
  assert.ok(html.indexOf(' open') === -1, 'the tail is closed');
});
check('a reason is escaped, so a verify line cannot inject markup', () => {
  const html = symphonyBootstrapHtml(boot({state: 'failed', label: 'Failed', reason: '<img src=x onerror=alert(1)>'}));
  assert.ok(html.indexOf('<img') === -1, html);
  assert.ok(html.indexOf('&lt;img') !== -1, html);
});
check('a budget that is used up says what resets it', () => {
  const html = symphonyBootstrapHtml(boot({state: 'budget_exhausted', label: 'Budget exhausted after 3 failed runs',
    reason: GEMFILE, reset_hint: 'A new commit to the env spec files resets the budget.'}));
  assert.ok(html.indexOf('A new commit to the env spec files resets the budget.') !== -1, html);
});
check('a ready cache says since when', () => {
  const html = symphonyBootstrapHtml(boot({state: 'ready', label: 'Ready for commit abc1234',
    ready_since: '2026-10-15T09:00:00+00:00', commit: 'abc1234'}));
  assert.ok(html.indexOf('Since ') !== -1, html);
});
check('the history lists each attempt, newest first, with its outcome, commit and reason', () => {
  const html = symphonyBootstrapHtml(boot({state: 'failed', label: 'Failed', history: [
    {number: 7, commit: 'bbb2222', started_at: '2026-10-15T11:00:00+00:00', duration_seconds: 600,
      outcome: 'running', reason: null, backend: 'claude_code', model: 'sonnet', job_id: 'job-7'},
    {number: 6, commit: 'aaa1111', started_at: '2026-10-15T10:00:00+00:00', duration_seconds: 120,
      outcome: 'failed', reason: GEMFILE, backend: '', model: '', job_id: null}
  ]}));
  assert.ok(html.indexOf('History (2)') !== -1, html);
  assert.ok(html.indexOf('#7') < html.indexOf('#6'), 'newest first');
  assert.ok(html.indexOf('Running') !== -1 && html.indexOf('Failed after 2m') !== -1, html);
  assert.ok(html.indexOf('job job-7') !== -1, html);
});
check('a row the scope hides shows no bootstrap detail, its reason or its history', () => {
  const row = symphonyTableRowHtml(sym('other', {state: 'failed', label: 'Failed', reason: 'secret-ish ' + GEMFILE,
    history: [{number: 1, commit: 'x', outcome: 'failed', reason: GEMFILE}]}), 'mine');
  assert.ok(row.indexOf('Could not locate Gemfile') === -1, row);
  assert.ok(row.indexOf('History (') === -1, row);
  assert.ok(row.indexOf('Hidden: another symphony is shown') !== -1, row);
});
check('a visible row carries the bootstrap detail in its Env cell', () => {
  const row = symphonyTableRowHtml(sym('mine', {state: 'failed', label: 'Failed on run 1 of 3', reason: GEMFILE}), 'mine');
  assert.ok(row.indexOf(GEMFILE) !== -1, row);
});

// --- scope: each symphony's bootstrap travels with its own entry only -------------------------------- //
check('a scoped snapshot keeps only the chosen symphony, with its own bootstrap', () => {
  const snap = {symphonies: [sym('a', {state: 'bootstrapping', label: 'Bootstrapping, run 1 of 3'}),
    sym('b', {state: 'failed', label: 'Failed on run 1 of 3', reason: 'b-only reason'})],
    active_sessions: [], open_questions: [], token_usage: null, card_pipeline: {}, board_summary: {}};
  const scoped = symphonyScope(snap, 'a');
  assert.strictEqual(scoped.symphonies.length, 1);
  assert.strictEqual(scoped.symphonies[0].name, 'a');
  assert.strictEqual(scoped.symphonies[0].env_bootstrap.state, 'bootstrapping');
  assert.strictEqual(JSON.stringify(scoped).indexOf('b-only reason'), -1);
});

// --- the status strip -------------------------------------------------------------------------------- //
check('the strip names one running bootstrap in the server words', () => {
  const text = bootstrapStripText({symphonies: [sym('a', {state: 'bootstrapping', label: 'Bootstrapping, run 2 of 3'})]});
  assert.strictEqual(text, 'Env bootstrap: Bootstrapping, run 2 of 3');
});
check('the strip adds the reason when the bootstrap is failing', () => {
  const text = bootstrapStripText({symphonies: [sym('a', {state: 'failed', label: 'Failed on run 1 of 3', reason: GEMFILE})]});
  assert.strictEqual(text, 'Env bootstrap: Failed on run 1 of 3 · ' + GEMFILE);
});
check('the strip counts several bootstraps and does not name a symphony', () => {
  const text = bootstrapStripText({symphonies: [sym('a', {state: 'bootstrapping', label: 'x'}), sym('b', {state: 'failed', label: 'y'})]});
  assert.strictEqual(text, 'Env bootstrap: 2 symphonies need attention');
});
check('the strip says nothing while every bootstrap is ready or not configured', () => {
  assert.strictEqual(bootstrapStripText({symphonies: [sym('a', {state: 'ready', label: 'Ready'}), sym('b', {state: 'not_configured', label: 'Not configured'})]}), '');
  assert.strictEqual(bootstrapStripText(null), '');
});

// --- a held card -------------------------------------------------------------------------------------- //
check('a card held by the bootstrap waits with the server words', () => {
  const card = {id: 'card-1', column: 'TODO', has_session: true};
  const snap = {session_skip_reasons: {'card-1': {reason: 'env_bootstrap',
    detail: 'Waiting for the environment bootstrap (run 2 of 3)'}}};
  const reason = waitingReason(card, snap);
  assert.strictEqual(reason.kind, 'queued');
  assert.strictEqual(reason.text, 'Waiting for the environment bootstrap (run 2 of 3)');
});

// --- the feed ----------------------------------------------------------------------------------------- //
check('each bootstrap feed event has a chip, a summary and an icon of its own', () => {
  ['env_bootstrap_dispatched', 'env_bootstrap_failed', 'env_bootstrap_retry', 'env_bootstrap_budget_reset',
    'env_bootstrap_ready', 'env_bootstrap_exhausted'].forEach((type) => {
    assert.strictEqual(afLabel(type), 'BOOTSTRAP', type);
    assert.notStrictEqual(afSummary({activity_type: type}), 'Activity reported.', type);
    assert.notStrictEqual(afIcon(type), '*', type);
  });
});
check('a feed event for a bootstrap is a plain lifecycle row', () => {
  assert.strictEqual(afRowKind({activity_type: 'env_bootstrap_failed'}), 'lifecycle');
});

check('the bootstrap output has no dash characters', () => {
  noDashes(symphonyBootstrapHtml(boot({state: 'failed', label: 'Failed on run 1 of 3', reason: GEMFILE,
    history: [{number: 1, commit: 'a', outcome: 'failed', reason: GEMFILE}]})));
  noDashes(bootstrapStripText({symphonies: [sym('a', {state: 'failed', label: 'Failed on run 1 of 3', reason: GEMFILE})]}));
});

// --- the feed under a symphony: a bootstrap row belongs to its symphony -------------------------------- //
check('a bootstrap row shows only under its own symphony, or under All', () => {
  const row = {activity_type: 'env_bootstrap_failed', card_id: '', symphony: 'beta', stage: 'env_bootstrap'};
  assert.strictEqual(symphonyKeepsFeedEntry({}, 'alpha', row), false, 'alpha must not see beta');
  assert.strictEqual(symphonyKeepsFeedEntry({}, 'beta', row), true, 'beta sees its own');
  assert.strictEqual(symphonyKeepsFeedEntry({}, '', row), true, 'All sees every symphony');
});
check('a card row with no card keeps its existing behaviour (kept under any choice)', () => {
  const row = {activity_type: 'progress', card_id: '', symphony: '', stage: ''};
  assert.strictEqual(symphonyKeepsFeedEntry({}, 'alpha', row), true);
});
check('a card row follows its card owner', () => {
  const snap = {symphonies: [{name: 'alpha', state: {board_snapshot: {TODO: ['card-1']}}},
    {name: 'beta', state: {board_snapshot: {TODO: ['card-2']}}}]};
  assert.strictEqual(symphonyKeepsFeedEntry(snap, 'alpha', {card_id: 'card-2', symphony: ''}), false);
  assert.strictEqual(symphonyKeepsFeedEntry(snap, 'alpha', {card_id: 'card-1', symphony: ''}), true);
});

// --- the System health row under a chosen symphony ----------------------------------------------------- //
function healthFixture() {
  return {overall: 'degraded', rows: [{key: 'disk_env_cache', name: 'Disk and env cache', status: 'degraded',
    reason: 'env cache bootstrap failed for 2 symphonies: alpha: Could not locate Gemfile; beta: rbenv missing',
    env_failures: [{symphony: 'alpha', reason: 'Could not locate Gemfile'}, {symphony: 'beta', reason: 'rbenv missing'}]},
    {key: 'github_api', name: 'GitHub API', status: 'healthy', reason: null}]};
}
check('under alpha, the row names only alpha failure and counts the other', () => {
  const row = symphonyHealthScope(healthFixture(), 'alpha').rows[0];
  assert.strictEqual(row.reason, 'env cache bootstrap failed: Could not locate Gemfile; 1 other symphony has a failing bootstrap');
  assert.strictEqual(row.reason.indexOf('rbenv'), -1, 'beta reason must not show under alpha');
});
check('under a symphony with no failure, the row says only that others are failing', () => {
  const row = symphonyHealthScope(healthFixture(), 'gamma').rows[0];
  assert.strictEqual(row.reason, 'env cache bootstrap failed: 2 other symphonies have failing bootstraps');
});
check('under All, the row keeps every reason', () => {
  const health = healthFixture();
  assert.strictEqual(symphonyHealthScope(health, '').rows[0].reason, health.rows[0].reason);
});
check('a row without env failures, and a health object without rows, pass through unchanged', () => {
  const health = healthFixture();
  assert.strictEqual(symphonyHealthScope(health, 'alpha').rows[1], health.rows[1]);
  assert.strictEqual(symphonyHealthScope(null, 'alpha'), null);
});
check('the passed-not-ready state has its own word on the Symphonies page', () => {
  const env = symphonyEnvView(sym('a', {state: 'passed_not_ready', label: 'Passed, not ready'}));
  assert.strictEqual(env.label, 'Passed, not ready');
});

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log('bootstrap_266_checks: ' + passed + ' passed');
