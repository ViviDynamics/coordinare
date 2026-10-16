const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const response = {
  window: '7d', start: '2026-10-08T12:00:00Z', end: '2026-10-15T12:00:00Z',
  partial: false, partial_reasons: [], definitions: {},
  totals: {runs: 3, tokens: {total: 900}, cost: {usd: 1.25}, missing_usage_runs: 0,
    outcomes: {system_error: 1}},
  groups: [{key: 'reviewing', runs: 3, success_rate: 0.5, first_pass_rate: 0.25,
    duration_seconds: {median: 60}, tokens: {total: 900}, cost: {usd: 1.25}}],
  daily: [{day: '2026-10-15', cost: {usd: 1.25}}],
  recent_runs: [{symphony: 'alpha', card: 'card', run: 'run', stage: 'reviewing',
    started_at: '2026-10-15T11:00:00Z', outcome: 'system_error'}],
  card_flow: {alpha: {cards_completed: 2, prs_merged: 2,
    cycle_time_seconds: {median: 120, p90: 180}, human_review_wait_seconds: 60,
    revision_rounds_per_pr: 1, cost_per_completed_card_usd: 0.625,
    columns_now: {TODO: 2, IN_PROGRESS: 1, IN_REVIEW: 1, DONE: 2, BLOCKED: 0, BACKLOG: 0},
    daily_completed: [{day: '2026-10-15', cards_completed: 2}],
    recent_completed: [{card: 'card', merged_at: '2026-10-15T11:30:00Z'}]}},
  symphony_health: {alpha: {name: 'alpha', enabled: true, cards_active: 1, cards_queued: 2,
    slots_in_use: 1, max_concurrent_cards: 3, last_poll_at: '2026-10-15T11:59:00Z',
    error_count: 0, env_status: {state: 'ready', reason: 'Ready'}, state: {}}},
  reliability: {alpha: {checks: 2, success_rate: 0.5, recent_errors: [
    {at: '2026-10-15T10:00:00Z', reason: '<script>bad</script>'}]}}
};
const sym = response.symphony_health.alpha;
const html = symphonyStatsHtml('alpha', response, response);
['Throughput', 'Flow', 'Spend', 'Reliability', 'Now', 'Recent runs', 'Recent completed cards',
 '50%', '25%', 'title=', '/cards/alpha/card', '/runs/alpha/card/run', 'h.symphony=alpha',
 'Env', '1 of 3'].forEach(text => assert.ok(html.includes(text), text));
assert.ok(!html.includes('<script>bad'));
assert.ok(html.includes('&lt;script&gt;bad'));
assert.ok(!/<button|<input|<form|sym-config-form/.test(html), 'statistics remain read-only');
assert.ok(html.includes('restart-reconcile'));
assert.ok(!html.includes('sym-env-bootstrap-btn') && !html.includes('sym-wiki-init-btn'));
assert.ok(/role="img"/.test(html) && /<title>/.test(html));
const noGroup = {...response, group_by: 'symphony', groups: []};
assert.strictEqual(symphonyStatsHeadline(noGroup, 'alpha').spend, 'Not recorded');
const headline = symphonyStatsHeadline(response, 'alpha');
assert.strictEqual(headline.completed, '2');
assert.strictEqual(headline.spend, '$1.25');
assert.strictEqual(headline.active, '1');
assert.strictEqual(headline.queued, '2');
const partial = JSON.parse(JSON.stringify(response));
partial.partial = true;
partial.partial_reasons = ['usage_partial_line'];
const partialHtml = symphonyStatsHtml('alpha', partial, partial);
assert.strictEqual(symphonyStatsHeadline(partial, 'alpha').spend, 'partial');
assert.ok(!partialHtml.includes('$1.25'));
assert.ok(!partialHtml.includes('>900<'));
assert.ok(partialHtml.includes('partial'));
const missing = JSON.parse(JSON.stringify(response));
missing.totals.missing_usage_runs = 1;
assert.strictEqual(symphonyStatsHeadline(missing, 'alpha').spend, 'partial');
const empty = JSON.parse(JSON.stringify(response));
empty.symphony_health.alpha.enabled = false;
empty.symphony_health.alpha.last_poll_at = null;
empty.reliability.alpha = {checks: 0, success_rate: null, recent_errors: []};
const emptyHtml = symphonyStatsHtml('alpha', empty, empty);
assert.ok(emptyHtml.includes('Disabled') && emptyHtml.includes('Not recorded'));
empty.reliability.alpha.reason = 'board_check_read_unavailable: <locked>';
const unavailableHtml = symphonyStatsHtml('alpha', empty, empty);
assert.ok(unavailableHtml.includes('Not recorded: board_check_read_unavailable: &lt;locked&gt;'));
assert.ok(!unavailableHtml.includes('<locked>'));
assert.ok(!/[\u2013\u2014]/.test(html));
assert.ok(statsChartHtml([{label: '<bad>', value: null}], 'Cost').includes('Not recorded'));
// Daily symphony charts retain the reviewed shared renderer's scale and labels.
const costs = statsChartHtml([
  {x: 100, label: 'First day', value: 0.02},
  {x: 300, label: 'Last day', value: 0.05}
], 'Daily cost');
assert.ok(costs.includes('cx="6" cy="42"'));
assert.ok(costs.includes('cx="314" cy="6"'));
assert.ok(costs.includes('aria-label="Daily cost. First day: 0.02; Last day: 0.05"'));
const zeros = statsChartHtml([{x: 100, label: 'First day', value: 0}], 'Completed cards');
assert.ok(zeros.includes('First day: 0'));
assert.ok(!/NaN|Infinity/.test(zeros));
console.log('Symphony statistics contracts passed');

async function actionChecks() {
  const elements = {};
  ['sym-env-bootstrap-btn', 'sym-env-bootstrap-msg', 'sym-wiki-init-btn', 'sym-wiki-init-msg'].forEach(id => {
    elements[id] = {disabled: false, textContent: ''};
  });
  global.document = {getElementById: id => elements[id]};
  global.location = {pathname: '/symphonies/alpha'};
  let confirmed = false;
  const confirmations = [];
  global.window = {confirm: text => {confirmations.push(text); return confirmed;}};
  let status = 202;
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({url, options});
    return {status, ok: status === 202, json: async () => ({error: 'Already in flight'})};
  };
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const bootstrap = elements['sym-env-bootstrap-btn'];
  const wiki = elements['sym-wiki-init-btn'];
  const bootstrapMessage = elements['sym-env-bootstrap-msg'];
  const wikiMessage = elements['sym-wiki-init-msg'];
  wireSymphonyDetailActions('alpha/a b', {querySelector: selector => elements[selector.slice(1)]});
  wireSymphonyDetailActions('alpha/a b', {querySelector: selector => elements[selector.slice(1)]});
  bootstrap.onclick();
  assert.strictEqual(calls.length, 0, 'cancelled bootstrap confirmation must not POST');
  assert.ok(confirmations[0].includes('retry budget'));
  confirmed = true;
  bootstrap.onclick();
  assert.strictEqual(bootstrap.disabled, true);
  await settle();
  assert.ok(calls[0].url.endsWith('/alpha%2Fa%20b/env-bootstrap'));
  assert.strictEqual(bootstrap.disabled, false);
  const beforeWiki = confirmations.length;
  wiki.onclick();
  assert.strictEqual(wiki.disabled, true);
  await settle();
  assert.ok(calls[1].url.endsWith('/alpha%2Fa%20b/wiki-init'));
  assert.ok(wikiMessage.textContent.includes('Accepted'));
  assert.strictEqual(confirmations.length, beforeWiki, 'wiki keeps its existing immediate action behavior');
  for (const denied of [401, 403]) {
    status = denied;
    bootstrap.onclick();
    wiki.onclick();
    await settle();
    const expected = denied === 401 ? 'Sign in' : 'permission';
    assert.ok(bootstrapMessage.textContent.includes(expected));
    assert.ok(wikiMessage.textContent.includes(expected));
    assert.strictEqual(bootstrap.disabled, false);
    assert.strictEqual(wiki.disabled, false);
  }
  status = 409;
  wiki.onclick();
  await settle();
  assert.strictEqual(wikiMessage.textContent, 'Already in flight');
  global.fetch = async () => {throw new Error('offline');};
  wiki.onclick();
  await settle();
  assert.strictEqual(wikiMessage.textContent, 'Network error');
  assert.strictEqual(wiki.disabled, false);
  assert.ok(calls.every(call => call.options.method === 'POST' && call.options.credentials === 'same-origin'));
  console.log('Symphony operational action checks passed');
}

async function requestChecks() {
  await actionChecks();
  let scope = 'alpha';
  const writes = [];
  const elements = {
    'sym-stats-content': {set innerHTML(value) { writes.push(value); }},
    'sym-stats-window': {addEventListener() {}}
  };
  global.location = {pathname: '/symphonies/alpha'};
  global.document = {getElementById: id => elements[id] || null};
  global.currentScopedState = () => ({symphonies: [{name: scope}]});
  global.currentSymphonyChoice = () => scope;
  const calls = [];
  global.fetch = (url, options) => new Promise(resolve => calls.push({url, options, resolve}));
  const page = {innerHTML: '', name: '', getAttribute() {return this.name;}, setAttribute(key, value) {this.name = value;}};
  const settle = () => new Promise(resolve => setImmediate(resolve));
  scope = 'beta';
  loadSymphonyStatsDetail('alpha', page, false, 'initial');
  assert.strictEqual(calls.length, 0, 'scoped detail must not fetch another symphony');
  assert.ok(page.innerHTML.includes('Choose All symphonies'));
  scope = 'alpha';
  loadSymphonyStatsDetail('alpha', page, false, 'initial');
  loadSymphonyStatsDetail('alpha', page, false, 'initial');
  assert.strictEqual(calls.length, 2, 'in flight detail reads are reused');
  assert.ok(calls.every(call => call.url.includes('symphony=alpha') && call.options.credentials === 'same-origin'));
  loadSymphonyStatsDetail('alpha', page, false, 'board-changed');
  assert.strictEqual(calls.length, 4, 'board changes refresh current counts');
  calls[2].resolve({ok: true, json: () => Promise.resolve(response)});
  calls[3].resolve({ok: true, json: () => Promise.resolve(response)});
  await settle();
  assert.strictEqual(writes.length, 1);
  calls[0].resolve({ok: true, json: () => Promise.resolve(partial)});
  calls[1].resolve({ok: true, json: () => Promise.resolve(partial)});
  await settle();
  assert.strictEqual(writes.length, 1, 'stale responses cannot replace newer data');
  loadSymphonyStatsDetail('alpha', page, false, 'board-changed');
  assert.strictEqual(calls.length, 4, 'fresh detail reads are not repeated on each SSE');
  _symphonyStatsWindow = '24h';
  loadSymphonyStatsDetail('alpha', page, true, 'board-changed');
  assert.ok(calls[4].url.includes('window=24h'));
  calls[4].resolve({ok: false});
  calls[5].resolve({ok: false});
  await settle();
  assert.ok(writes[writes.length - 1].includes('Statistics unavailable'));
  const cells = {};
  ['active', 'queued', 'completed', 'spend'].forEach(key => {
    cells[key] = {textContent: '', getAttribute: name => name === 'data-stats-measure' ? key : 'Definition'};
  });
  const row = {getAttribute: () => 'alpha', querySelectorAll: () => Object.values(cells)};
  const hiddenCell = {textContent: 'Hidden'};
  const hiddenRow = {getAttribute: () => 'beta', querySelectorAll: () => [hiddenCell]};
  const table = {querySelectorAll: () => [row, hiddenRow]};
  global.location.pathname = '/symphonies';
  loadSymphonyListStats(table, 'alpha', 'one');
  loadSymphonyListStats(table, 'alpha', 'one');
  assert.strictEqual(calls.length, 7, 'list updates share one cached request');
  const listed = JSON.parse(JSON.stringify(response));
  listed.group_by = 'symphony';
  listed.groups = [{key: 'alpha', ...listed.totals}];
  calls[6].resolve({ok: true, json: () => Promise.resolve(listed)});
  await settle();
  assert.strictEqual(cells.completed.textContent, headline.completed);
  assert.strictEqual(cells.spend.textContent, headline.spend);
  assert.strictEqual(cells.active.textContent, headline.active);
  assert.strictEqual(cells.queued.textContent, headline.queued);
  assert.strictEqual(hiddenCell.textContent, 'Hidden');
  assert.ok(cells.spend.title.includes('7d'));
  global.location.pathname = '/symphonies/alpha';
  loadSymphonyStatsDetail('alpha', page, true, 'return');
  const leaving = calls.length;
  global.location.pathname = '/performers';
  calls[leaving - 2].resolve({ok: true, json: () => Promise.resolve(response)});
  calls[leaving - 1].resolve({ok: true, json: () => Promise.resolve(response)});
  await settle();
  assert.strictEqual(_symphonyStatsLoading, false, 'leaving during a read must release the loading guard');
  global.location.pathname = '/symphonies/alpha';
  loadSymphonyStatsDetail('alpha', page, false, 'return');
  assert.strictEqual(calls.length, leaving + 2, 'returning after an abandoned read must load again');
  console.log('Symphony statistics request and scope checks passed');
}
requestChecks().catch(error => {console.error(error); process.exitCode = 1;});
