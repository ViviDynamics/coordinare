// 156: checks for the Symphonies table's pure functions: the GitHub link validation, the Status
// reason, the Env words and the row markup. The functions run as shipped. The bundle is helpers.js,
// usage.js and symphonies.js, concatenated by tests/unit/test_156_symphonies_table_js.py.
//
// Usage: node symphonies_table_156_checks.js <bundle.js>

const fs = require('fs');
const assert = require('assert');

const elements = {};
var document = {getElementById(id) {return elements[id] || null;}};
var _lastState = null;
eval(fs.readFileSync(process.argv[2], 'utf8'));

const failures = [];
let passed = 0;
function check(name, run) {
  try {run(); passed++;} catch (error) {failures.push(name + ': ' + error.message);}
}
const EM = String.fromCharCode(0x2014);
const EN = String.fromCharCode(0x2013);
function noDashes(html) {
  assert.ok(html.indexOf(EM) === -1 && html.indexOf(EN) === -1, 'a dash in: ' + html);
}
function sym(extra) {
  return Object.assign({name: 'sym', github_project_number: 16, github_org: 'acme', repo: 'sym-repo',
    priority: 0, cycle_count: 2, error_count: 0, last_poll_at: '2026-10-14T11:00:00+00:00',
    poll_interval_seconds: 30, daemon_cycles_completed: 2, state: null, enabled: true,
    env_bootstrap_performer_id: null, cards_active: 0, cards_queued: 0, cards_paused: 0,
    env_status: {state: 'not_configured', reason: 'No env bootstrap performer is configured for this symphony.'},
    spend_week: {tracked: true, reason: null, tokens: {total: 2050},
      cost: {usd: 0.13, partial_usd: 0.13, kind_usd: {}, unknown_reason: null}, models: [],
      pricing: {as_of: '2026-10-01', source: 'table'}}},
    extra || {});
}

// --- the GitHub links: only validated owners, repositories and numbers ---------------- //
check('a valid owner, repository and project number link to GitHub', () => {
  const id = symphonyIdentity(sym());
  assert.strictEqual(id.projectUrl, 'https://github.com/orgs/acme/projects/16');
  assert.strictEqual(id.repoUrl, 'https://github.com/acme/sym-repo');
});
check('a value that is not a valid GitHub name is shown but never linked', () => {
  const id = symphonyIdentity(sym({github_org: 'acme/../evil', repo: 'x"onmouseover=1'}));
  assert.strictEqual(id.projectUrl, '');
  assert.strictEqual(id.repoUrl, '');
  assert.strictEqual(id.repoText, 'acme/../evil/x"onmouseover=1');
});
check('an owner with a trailing dash, a dot-dot repository or a bad project number is not linked', () => {
  assert.strictEqual(symphonyIdentity(sym({github_org: 'acme-'})).projectUrl, '');
  assert.strictEqual(symphonyIdentity(sym({repo: '..'})).repoUrl, '');
  assert.strictEqual(symphonyIdentity(sym({github_project_number: 1.5})).projectUrl, '');
  assert.strictEqual(symphonyIdentity(sym({github_project_number: 0})).projectUrl, '');
  assert.strictEqual(symphonyIdentity(sym({github_project_number: '16'})).projectUrl, '');
});
check('a missing owner leaves the project unlinked and still names it', () => {
  const id = symphonyIdentity(sym({github_org: ''}));
  assert.strictEqual(id.projectUrl, '');
  assert.strictEqual(id.projectText, 'GitHub Project #16');
});

// --- Status ---------------------------------------------------------------------------- //
check('a disabled symphony is Disabled, with the config reason', () => {
  const status = symphonyStatus(sym({enabled: false}));
  assert.strictEqual(status.label, 'Disabled');
  assert.ok(status.reason.indexOf('enabled: false') === 0);
});
check('a symphony whose first check failed is Error, with the redacted error', () => {
  const status = symphonyStatus(sym({last_poll_at: null, cycle_count: 0, error_count: 3,
    daemon_cycles_completed: 1, state: {last_error: 'GitHub 401 Bad credentials'}}));
  assert.strictEqual(status.label, 'Error');
  assert.strictEqual(status.reason, 'Last check failed: GitHub 401 Bad credentials');
  assert.strictEqual(status.tone, 'red');
});
check('a checked symphony with failing checks is Error, with its last error', () => {
  const status = symphonyStatus(sym({error_count: 2, state: {last_error: 'timeout'}}));
  assert.strictEqual(status.label, 'Error');
  assert.strictEqual(status.reason, 'Last check failed: timeout');
});
check('a checked symphony with failing checks and no recorded error still says how many', () => {
  const status = symphonyStatus(sym({error_count: 2, state: null}));
  assert.strictEqual(status.label, 'Error');
  assert.ok(status.reason.indexOf('2 checks failed in a row') === 0);
});
check('a symphony with cards the board paused is Paused, with the count', () => {
  assert.strictEqual(symphonyStatus(sym({cards_paused: 1})).reason, '1 card is paused on the board.');
  const two = symphonyStatus(sym({cards_paused: 2}));
  assert.strictEqual(two.label, 'Paused');
  assert.strictEqual(two.reason, '2 cards are paused on the board.');
});
check('a disabled symphony outranks an error and a pause', () => {
  assert.strictEqual(symphonyStatus(sym({enabled: false, error_count: 4, cards_paused: 1})).label, 'Disabled');
});
check('an error outranks a pause', () => {
  assert.strictEqual(symphonyStatus(sym({error_count: 1, cards_paused: 1})).label, 'Error');
});
check('an active symphony says how often it is checked', () => {
  const status = symphonyStatus(sym({}));
  assert.strictEqual(status.label, 'Active');
  assert.strictEqual(status.reason, 'Checked every 30 seconds.');
  assert.strictEqual(symphonyStatus(sym({poll_interval_seconds: null})).reason, 'Checked when a webhook arrives.');
});
check('a placeholder symphony nobody has checked is Active, and its reason is the #155 note', () => {
  const status = symphonyStatus(sym({name: 'placeholder', last_poll_at: null, cycle_count: 0,
    daemon_cycles_completed: 0, poll_interval_seconds: 30, state: null}));
  assert.strictEqual(status.label, 'Active');
  assert.strictEqual(status.reason, 'Not checked yet');
  const never = symphonyStatus(sym({last_poll_at: null, cycle_count: 0, daemon_cycles_completed: 4}));
  assert.strictEqual(never.reason, 'Never checked: check config');
});

// --- Env --------------------------------------------------------------------------------- //
check('each env state has its word', () => {
  const words = {not_configured: 'Not configured', bootstrapping: 'Bootstrapping', failed: 'Failed',
    ready: 'Ready', not_bootstrapped: 'Not bootstrapped'};
  Object.keys(words).forEach(state => {
    assert.strictEqual(symphonyEnvView(sym({env_status: {state: state, reason: 'why'}})).label, words[state]);
  });
});
check('an env state the snapshot does not name is Not known, not guessed', () => {
  assert.strictEqual(symphonyEnvView(sym({env_status: {state: 'weird'}})).label, 'Not known');
  assert.strictEqual(symphonyEnvView(sym({env_status: undefined})).label, 'Not known');
});

// --- counts ------------------------------------------------------------------------------ //
check('a count the snapshot does not carry says Not known, and a zero says zero', () => {
  assert.strictEqual(symphonyCountText(undefined), 'Not known');
  assert.strictEqual(symphonyCountText(0), '0');
  assert.strictEqual(symphonyCountText(NaN), 'Not known');
});

// --- the row ----------------------------------------------------------------------------- //
check('a row opens the symphony page, escapes its name and carries its own spend', () => {
  const html = symphonyTableRowHtml(sym({name: 'a<b', cards_active: 1, cards_queued: 2}), '');
  assert.ok(html.indexOf('data-href="/symphonies/a%3Cb"') !== -1, html);
  assert.ok(html.indexOf('a&lt;b') !== -1 && html.indexOf('<b>') === -1, html);
  assert.ok(html.indexOf('$0.13') !== -1, html);
  noDashes(html);
});
check('the row shows its status with the reason, and the env words with their reason', () => {
  const html = symphonyTableRowHtml(sym({name: 'placeholder', last_poll_at: null, cycle_count: 0,
    daemon_cycles_completed: 0, env_status: {state: 'failed', reason: 'pull denied'},
    env_bootstrap_performer_id: 'perf'}), '');
  assert.ok(html.indexOf('Active') !== -1 && html.indexOf('Not checked yet') !== -1, html);
  assert.ok(html.indexOf('Failed') !== -1 && html.indexOf('pull denied') !== -1, html);
  assert.ok(html.indexOf('sym-row-bootstrap') !== -1, html);
  noDashes(html);
});
check('a symphony that is not the chosen one shows no spend or card counts', () => {
  const html = symphonyTableRowHtml(sym({name: 'alpha', cards_active: 5, cards_queued: 6}), 'beta');
  assert.ok(html.indexOf('$0.13') === -1, html);
  assert.ok(html.indexOf('Hidden: another symphony is shown') !== -1, html);
  assert.strictEqual(symphonyRowHidden('alpha', 'beta'), true);
  assert.strictEqual(symphonyRowHidden('beta', 'beta'), false);
  assert.strictEqual(symphonyRowHidden('beta', ''), false);
});
check('the chosen symphony keeps its own figures', () => {
  const html = symphonyTableRowHtml(sym({name: 'beta', cards_active: 3, cards_queued: 4}), 'beta');
  assert.ok(html.indexOf('$0.13') !== -1 && html.indexOf('Hidden') === -1, html);
});
check('a row with no project, repository or spend says so in words', () => {
  const html = symphonyTableRowHtml(sym({github_project_number: null, repo: '', github_org: '', spend_week: null}), '');
  assert.ok(html.indexOf('No project number') !== -1 && html.indexOf('No repository') !== -1, html);
  noDashes(html);
});
check('a disabled symphony row says why in its status cell', () => {
  const html = symphonyTableRowHtml(sym({enabled: false}), '');
  assert.ok(html.indexOf('enabled: false') !== -1, html);
  assert.ok(html.indexOf('>Disabled<') !== -1, html);
});

// --- review: recovery and scope -------------------------------------------------------- //
check('a failure then a success reads Active, and the error is no longer shown', () => {
  // The daemon clears error_count and last_error on a successful cycle (services/symphony_failures.py).
  const recovered = sym({error_count: 0, state: {last_error: null}, last_poll_at: '2026-10-14T11:59:00+00:00'});
  assert.strictEqual(symphonyStatus(recovered).label, 'Active');
  assert.ok(symphonyTableRowHtml(recovered, '').indexOf('>Active<') !== -1);
});
check('a scoped row hides another symphony error text and env reason, and keeps the words', () => {
  const other = sym({name: 'other', error_count: 2, state: {last_error: 'secret-project-id 123 quota'},
    env_status: {state: 'failed', reason: 'bootstrap failed: private repo path /srv/other'}});
  const html = symphonyTableRowHtml(other, 'mine');
  assert.ok(html.indexOf('quota') === -1, html);
  assert.ok(html.indexOf('secret-project-id') === -1, html);
  assert.ok(html.indexOf('/srv/other') === -1, html);
  assert.ok(html.indexOf('>Error<') !== -1 && html.indexOf('Failed<div') !== -1, html);
  assert.ok(html.indexOf('Hidden: another symphony is shown') !== -1, html);
});
check('a scoped row hides the unchecked note, which can carry the last error', () => {
  const other = sym({name: 'other', last_poll_at: null, cycle_count: 0, error_count: 3,
    daemon_cycles_completed: 1, state: {last_error: 'token ghp_leak'}});
  const html = symphonyTableRowHtml(other, 'mine');
  assert.ok(html.indexOf('ghp_leak') === -1, html);
  assert.ok(html.indexOf('Last check failed') === -1, html);
});
check('the chosen symphony keeps its own error and env reason', () => {
  const mine = sym({name: 'mine', error_count: 2, state: {last_error: 'timeout'},
    env_status: {state: 'failed', reason: 'pull denied'}});
  const html = symphonyTableRowHtml(mine, 'mine');
  assert.ok(html.indexOf('Last check failed: timeout') !== -1 && html.indexOf('pull denied') !== -1, html);
});
check('no scope shows every reason', () => {
  const other = sym({name: 'other', error_count: 2, state: {last_error: 'visible under all'}});
  assert.ok(symphonyTableRowHtml(other, '').indexOf('visible under all') !== -1);
});

if (failures.length > 0) {
  failures.forEach(f => console.log('FAIL ' + f));
}
console.log('SUMMARY ' + passed + ' ' + failures.length);
process.exit(failures.length === 0 ? 0 : 1);
