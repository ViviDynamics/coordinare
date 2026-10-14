// 151: checks for the System health card's rendering (systemhealth.js), executed against
// the sliced shipped source the Python test writes to disk.
//
// Usage: node system_health_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node system_health_checks.js <sliced-js>");
  process.exit(1);
}
const src = fs.readFileSync(path.resolve(jsPath), "utf8");

let passed = 0;
let failed = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
function checkContains(name, actual, fragment) {
  if (String(actual).indexOf(fragment) !== -1) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: ${JSON.stringify(actual)} does not contain ${JSON.stringify(fragment)}`);
  }
}
function checkAbsent(name, actual, fragment) {
  if (String(actual).indexOf(fragment) === -1) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: ${JSON.stringify(actual)} should not contain ${JSON.stringify(fragment)}`);
  }
}
function checkMatches(name, actual, pattern) {
  if (pattern.test(String(actual))) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: ${JSON.stringify(actual)} does not match ${pattern}`);
  }
}

eval(src);

const agoMs = (ms) => new Date(Date.now() - ms).toISOString();
const row = (key, name, status, extra) => Object.assign(
  {key: key, name: name, status: status, summary: 'summary of ' + name, reason: null, fresh_label: 'last checked', checked_at: null, last_success_at: null},
  extra || {}
);
const allGreen = {overall: 'healthy', rows: [
  row('github_api', 'GitHub API', 'healthy', {checked_at: agoMs(5000), fresh_label: 'last call'}),
  row('github_webhooks', 'GitHub webhooks', 'not_monitored', {reason: 'coordinare polls the board on a timer'}),
  row('board_check', 'Board check', 'healthy', {checked_at: agoMs(10000), last_success_at: agoMs(10000)}),
  row('performer_runtime', 'Performer runtime', 'healthy', {checked_at: agoMs(40000)}),
  row('model_backends', 'Model backends', 'healthy', {checked_at: agoMs(50000)}),
  row('disk_env_cache', 'Disk and env cache', 'healthy', {fresh_label: 'measured', checked_at: agoMs(0)}),
]};

// --- the one-line summary -------------------------------------------------------------
const plainGreen = {overall: 'healthy', rows: allGreen.rows.filter(function(r) { return r.status !== 'not_monitored'; })};
const greenSum = systemHealthSummary(plainGreen);
check('an all-green card is one line, closed', greenSum.open, false);
check('an all-green card says all systems healthy', greenSum.text, 'All systems healthy');
check('an all-green card is tone healthy', greenSum.tone, 'healthy');
checkContains('the all-green markup is a closed details', systemHealthHtml(plainGreen), '<details>');
checkAbsent('the all-green markup does not open the details', systemHealthHtml(plainGreen), '<details open');

const noteSum = systemHealthSummary(allGreen);
check('not monitored never reads as all green', noteSum.tone, 'note');
check('not monitored is named in the one line', noteSum.text, 'All monitored systems healthy · 1 not monitored');
check('not monitored does not open the card', noteSum.open, false);

const oneDegraded = {overall: 'degraded', rows: [
  row('github_api', 'GitHub API', 'healthy'),
  row('board_check', 'Board check', 'degraded', {reason: 'last board check failed (TransientGitHubError)', checked_at: agoMs(4000)}),
]};
const degSum = systemHealthSummary(oneDegraded);
check('one degraded subsystem opens the card', degSum.open, true);
check('one degraded subsystem is named with its status', degSum.text, '1 system needs attention: Board check (degraded)');
check('one degraded subsystem is tone degraded', degSum.tone, 'degraded');
checkContains('the degraded markup opens the details', systemHealthHtml(oneDegraded), '<details open>');
checkContains('the degraded row shows its reason', systemHealthHtml(oneDegraded), 'last board check failed (TransientGitHubError)');

const twoDown = {overall: 'down', rows: [
  row('github_api', 'GitHub API', 'down'),
  row('performer_runtime', 'Performer runtime', 'degraded'),
]};
const downSum = systemHealthSummary(twoDown);
check('a down subsystem makes the tone down', downSum.tone, 'down');
check('several problems are counted and named', downSum.text, '2 systems need attention: GitHub API (down), Performer runtime (degraded)');

const unknownOnly = {overall: 'unknown', rows: [row('board_check', 'Board check', 'unknown'), row('github_api', 'GitHub API', 'healthy')]};
const unknownSum = systemHealthSummary(unknownOnly);
check('a card with nothing checked yet stays closed: it is not a problem', unknownSum.open, false);
check('a card with nothing checked yet names the count on the line', unknownSum.text, 'No problems seen · 1 awaiting first check');
check('a card with nothing checked yet is not tone healthy', unknownSum.tone, 'note');

// --- the rows -------------------------------------------------------------------------
const freshRow = systemHealthRowHtml(row('board_check', 'Board check', 'healthy', {
  checked_at: agoMs(125000), fresh_label: 'last checked', last_success_at: agoMs(125000),
}));
checkMatches('freshness reads how long ago the status was checked', freshRow, /last checked 2m \d+s ago/);
checkMatches('freshness reads how long ago the last success was', freshRow, /last success 2m \d+s ago/);
checkContains('freshness ticks: the checked time is an elapsed readout', freshRow, 'data-elapsed-since="');
checkContains('freshness ticks with its prefix', freshRow, 'data-elapsed-prefix="last checked "');
checkContains('freshness ticks with its suffix', freshRow, 'data-elapsed-suffix=" ago"');

const notMonitoredRow = systemHealthRowHtml(row('github_webhooks', 'GitHub webhooks', 'not_monitored', {
  summary: 'not in use', reason: 'coordinare polls the board on a timer',
}));
checkContains('a not-monitored row says not monitored in words', notMonitoredRow, 'not monitored');
checkContains('a not-monitored row says why', notMonitoredRow, 'coordinare polls the board on a timer');
checkAbsent('a not-monitored row has no freshness, since it observes nothing', notMonitoredRow, 'data-elapsed-since');

const unknownRow = systemHealthRowHtml(row('model_backends', 'Model backends', 'unknown'));
checkContains('an unknown row says not checked yet', unknownRow, 'not checked yet');

const healthyRow = systemHealthRowHtml(row('github_api', 'GitHub API', 'healthy'));
checkContains('a healthy row says healthy in words, not only in colour', healthyRow, '>healthy<');
check('the status words cover every status', Object.keys(SYSTEM_HEALTH_WORDS).sort(), ['degraded', 'down', 'healthy', 'not_monitored', 'unknown']);

// --- escaping ---------------------------------------------------------------------------
const hostile = '<img src=x onerror=alert(1)>';
const escapedRow = systemHealthRowHtml(row('github_api', hostile, 'degraded', {summary: hostile, reason: hostile, checked_at: agoMs(1000)}));
checkContains('a hostile name is escaped', escapedRow, '&lt;img src=x onerror=alert(1)&gt;');
checkAbsent('a hostile name is never markup', escapedRow, '<img');
const escapedCard = systemHealthHtml({overall: 'degraded', rows: [row('x', hostile, 'degraded')]});
checkAbsent('a hostile name in the summary is never markup', escapedCard, '<img');

// --- a card with no rows ----------------------------------------------------------------
checkContains('an empty card still renders a summary line', systemHealthHtml({overall: 'unknown', rows: []}), 'No systems reported');
check('an empty card is not reported as healthy', systemHealthSummary({overall: 'unknown', rows: []}).tone, 'note');
const awaitingAndNotMonitored = {overall: 'unknown', rows: [row('board_check', 'Board check', 'unknown'), row('github_webhooks', 'GitHub webhooks', 'not_monitored'), row('github_api', 'GitHub API', 'healthy')]};
check('awaiting and not monitored both appear on the one line, closed', systemHealthSummary(awaitingAndNotMonitored).text, 'No problems seen · 1 awaiting first check · 1 not monitored');
check('the awaiting-and-not-monitored line stays closed', systemHealthSummary(awaitingAndNotMonitored).open, false);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
