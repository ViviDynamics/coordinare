var assert = require('assert');
var fs = require('fs');
var vm = require('vm');
var root = 'src/coordinare/dashboard/dashboard_static/';
var context = {console: console, Intl: Intl, Date: Date, COORDINARE_VOCABULARY: {stage_agent: {reviewing: 'reviewer'}}};
vm.createContext(context);
['helpers', 'usage', 'personas', 'stats_charts', 'personas_stats'].forEach(function(name) {
  vm.runInContext(fs.readFileSync(root + name + '.js', 'utf8'), context);
});
var body = {
  window: '7d', start: '2026-10-08T00:00:00Z', end: '2026-10-15T00:00:00Z',
  totals: {runs: 4, success_rate: 0.75, first_pass_rate: 0.5, duration_seconds: {median: 150, p90: 222},
    tokens: {total: 660}, cost: {usd: 0.04}, cost_per_run_usd: 0.01, missing_usage_runs: 0,
    outcomes: {success: 3, failure: 1}, failure_reasons: [{reason: '<script>', runs: 1}]},
  groups: [{key: 'alpha', runs: 4, success_rate: 0.75, cost: {usd: 0.04}}],
  daily: [{day: '2026-10-14', runs: 4, success_rate: 0.75, cost: {usd: 0.04}}],
  definitions: {}, recent_runs: [{symphony: 'alpha', card: 'c/1', run: 'r', outcome: 'success',
    duration_seconds: 12, cost: {usd: 0.01}, started_at: '2026-10-14T12:00:00Z'}],
  persona_versions: [], runtime: {stage: 'reviewing', symphonies: [{symphony: 'alpha', persona_override: true, model: 'm', mode: 'fast', hosts: ['worker']}], activity: []}
};
context.runPathFor = function(s, c, r) { return '/runs/' + [s, c, r].map(encodeURIComponent).join('/'); };
var html = context.personaStatsHtml(body, 'reviewer', 'alpha');
['75%', '50%', '150.00s', '222.00s', '660', '$0.040000', '$0.010000', 'Override', 'worker', 'h.agent=reviewer', '/runs/alpha/c%2F1/r'].forEach(function(value) { assert(html.indexOf(value) >= 0, value); });
assert(html.indexOf('/history?symphony=alpha&amp;h.agent=reviewer') >= 0);
assert(html.indexOf('h.symphony') < 0);
assert(context.personaStatsRecent(body, 'reviewer', '').indexOf('/history?symphony=&amp;h.agent=reviewer') >= 0);
assert(context.personaStatsRecent(body, 'review&er', 'alpha + beta').indexOf('/history?symphony=alpha%20%2B%20beta&amp;h.agent=review%26er') >= 0);
assert(html.indexOf('<script>') < 0);
assert(html.indexOf('&lt;script&gt;') >= 0);
assert(html.indexOf('role="img"') >= 0);
assert(html.indexOf('<title>') >= 0);
assert(html.indexOf('2026-10-14') >= 0);
body.totals.missing_usage_runs = 1;
html = context.personaStatsHtml(body, 'reviewer', 'alpha');
assert(html.indexOf('partial') >= 0);
assert(html.indexOf('data-metric="tokens" title=') >= 0);
assert(!/data-metric="tokens"[^>]*>660</.test(html));
body.totals.runs = 0;
assert(context.personaStatsHtml(body, 'curator', '').indexOf('No runs in this window') >= 0);
assert(context.personaStatsQuery('reviewer', '24h', 'alpha').indexOf('agent=reviewer') >= 0);
assert(context.personaStatsQuery('reviewer', '24h', 'alpha').indexOf('symphony=alpha') >= 0);
console.log('Persona statistics rendering checks passed');

// Source coverage, nulls, zero cost and version marks must remain distinguishable.
body.totals.runs = 4;
body.totals.missing_usage_runs = 0;
body.totals.success_rate = null;
body.totals.cost.usd = 0;
body.totals.cost_per_run_usd = 0;
html = context.personaStatsHtml(body, 'reviewer', 'alpha');
assert(/data-metric="success_rate"[^>]*>not recorded</.test(html));
assert(/data-metric="cost"[^>]*>\$0</.test(html));
body.partial = true;
body.partial_reasons = ['usage_partial_line'];
html = context.personaStatsHtml(body, 'reviewer', 'alpha');
assert(/data-metric="tokens"[^>]*>partial</.test(html));
assert(/data-metric="cost"[^>]*>partial</.test(html));
assert(html.indexOf('(partial)') >= 0);
var chart = context.statsChart([{x: 0, label: 'a', value: 1}, {x: 1, label: 'b', value: null}, {x: 2, label: 'c', value: 2}], 'Gap', String, [{label: 'edit <bad>', x: 1}]);
assert((chart.match(/<path d="M/g) || []).length === 3);
assert(chart.indexOf('edit &lt;bad&gt;') >= 0);
assert(fs.statSync(root + 'stats_charts.js').size < 8192);

async function selectionChecks() {
  var target = {innerHTML: ''};
  var scope = {symphonies: [{name: 'alpha'}, {name: 'beta'}]};
  var requests = [];
  context.location = {pathname: '/personas', hash: '#reviewer'};
  var handlers = {};
  var stats = {};
  var controls = {value: '7d'};
  var refresh = {};
  var nodes = {'persona-stats': stats, 'persona-stats-content': target,
    'persona-stats-window': controls, 'persona-stats-refresh': refresh};
  function tab(role) {
    var attrs = {'data-persona-tab': role};
    return {getAttribute: function(key) { return attrs[key]; },
      setAttribute: function(key, value) { attrs[key] = value; },
      querySelector: function() { return null; },
      closest: function(selector) { return selector === '[data-persona-tab]' ? this : null; },
      focus: function() { this.focused = true; }};
  }
  var tabs = ['reviewer', 'implementer'].map(tab);
  var panels = tabs.map(function(tab) {
    var role = tab.getAttribute('data-persona-tab');
    var panel = {hidden: true, getAttribute: function() { return role; }, setAttribute: function() {},
      insertBefore: function(node) { node.parentNode = this; }};
    nodes['read-persona-' + role] = panel;
    return panel;
  });
  var workspace = {
    getAttribute: function() { return 'read'; },
    closest: function() { return null; },
    querySelector: function() { return tabs.filter(function(tab) { return tab.getAttribute('aria-selected') === 'true'; })[0]; },
    querySelectorAll: function(selector) {
      if (selector === '[data-persona-tab]') return tabs;
      if (selector === '[data-persona-panel]') return panels;
      return [];
    },
    insertAdjacentHTML: function(_position, html) {
      assert(html.indexOf('persona-stats-agent') < 0, 'the persona rail is the only agent selector');
      assert(html.indexOf('aria-live') < 0, 'a refresh must not re-announce the whole panel');
    },
    addEventListener: function(name, handler) { handlers[name] = handler; }
  };
  context.document = {getElementById: function(id) { return nodes[id] || null; }};
  context.pushRoute = function(path) { context.location.hash = path.slice(path.indexOf('#')); };
  context.pathWithSymphony = function(path) { return path; };
  context.currentScopedState = function() { return scope; };
  context.symphonyNames = function(s) { return s.symphonies.map(function(sym) { return sym.name; }); };
  context.fetch = function(url) {
    return new Promise(function(resolve, reject) { requests.push({url: url, resolve: resolve, reject: reject}); });
  };
  context.personaStatsMount(workspace);
  context.personaMount(workspace);
  assert.strictEqual(stats.parentNode, panels[0], 'statistics start inside the hash-selected panel');
  assert.strictEqual(panels[0].hidden, false);
  assert(requests[0].url.indexOf('agent=reviewer') >= 0);
  handlers.keydown({target: tabs[0], key: 'ArrowRight', preventDefault: function() {}});
  assert.strictEqual(context._personaStats.role, 'implementer');
  assert.strictEqual(stats.parentNode, panels[1], 'statistics follow keyboard rail selection');
  assert.strictEqual(panels[0].hidden, true);
  assert.strictEqual(panels[1].hidden, false);
  assert.strictEqual(context.location.hash, '#implementer');
  assert.strictEqual(tabs[1].focused, true);
  var latest = JSON.parse(JSON.stringify(body));
  latest.totals.runs = 19;
  requests[1].resolve({ok: true, json: function() { return latest; }});
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert(target.innerHTML.indexOf('>19 (partial)<') >= 0);
  requests[0].resolve({ok: true, json: function() { return body; }});
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert(target.innerHTML.indexOf('>19 (partial)<') >= 0);
  context.personaStatsRefresh(false);
  assert.strictEqual(requests.length, 2, 'automatic refresh throttles');
  scope = {symphonies: [{name: 'beta'}]};
  context.personaStatsRefresh(false);
  assert(requests[2].url.indexOf('symphony=beta') >= 0);
  requests[2].reject(new Error('offline'));
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert(target.innerHTML.indexOf('Use Refresh') >= 0);
  context.personaStatsRefresh(true);
  context.location.pathname = '/history';
  requests[3].resolve({ok: true, json: function() { return body; }});
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert(target.innerHTML.indexOf('Loading statistics') >= 0, 'response after leaving is ignored');
  context.location.pathname = '/personas';
  context.location.hash = '#reviewer';
  context.personaSelect(workspace, context.personaHashRole('read'), false);
  assert.strictEqual(stats.parentNode, panels[0], 'Back or a hash link restores statistics with its persona');
  assert(requests[4].url.indexOf('agent=reviewer') >= 0);
  handlers.click({target: tabs[1]});
  assert.strictEqual(stats.parentNode, panels[1], 'statistics follow mouse rail selection');
  assert(requests[5].url.indexOf('agent=implementer') >= 0);
  controls.value = '24h';
  controls.onchange();
  assert(requests[6].url.indexOf('window=24h') >= 0);
  refresh.onclick();
  assert.strictEqual(requests.length, 8, 'Refresh forces a request for the current persona');
  var config = Object.assign({}, workspace, {getAttribute: function() { return 'config'; }, onPersonaSelect: undefined});
  context.personaStatsMount(config);
  assert.strictEqual(config.onPersonaSelect, undefined, 'Config installs no statistics callback');
  context.personaSelect(config, 'reviewer', false);
  assert.strictEqual(requests.length, 8, 'Config selection does not fetch statistics');
  console.log('Persona statistics asynchronous selection checks passed');
}
selectionChecks().catch(function(error) { console.error(error); process.exitCode = 1; });

if (process.argv[2]) {
  var api = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  var rendered = context.personaStatsHtml(api, 'reviewer', 'alpha');
  function metricContains(key, text) {
    assert(new RegExp('data-metric="' + key + '"[^>]*>' + text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '</').test(rendered), key + ': ' + text);
  }
  metricContains('runs', String(api.totals.runs));
  metricContains('tokens', context.usageCount(api.totals.tokens.total));
  metricContains('cost', context.usageMoney(api.totals.cost.usd));
  metricContains('cost_per_run_usd', context.usageMoney(api.totals.cost_per_run_usd));
  metricContains('duration_seconds', api.totals.duration_seconds.median.toFixed(2) + 's');
  metricContains('duration_seconds', api.totals.duration_seconds.p90.toFixed(2) + 's');
  metricContains('success_rate', api.totals.success_rate == null ? 'not recorded' : (api.totals.success_rate * 100).toFixed(1).replace(/\.0$/, '') + '%');
  console.log('HTTP statistics values match rendered figures for ' + api.window);
}
