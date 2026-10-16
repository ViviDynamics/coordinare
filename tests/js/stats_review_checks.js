var assert = require('assert');
var fs = require('fs');
var vm = require('vm');
var root = 'src/coordinare/dashboard/dashboard_static/';
var context = {console: console, Intl: Intl, Date: Date};
vm.createContext(context);
['helpers', 'usage', 'stats_charts', 'personas_stats'].forEach(function(name) {
  vm.runInContext(fs.readFileSync(root + name + '.js', 'utf8'), context);
});
var checks = {
  zero: function() {
    var html = context.statsBars([{label: 'Blocked', value: 0}], 'Outcomes');
    assert(html.includes('<span>0</span>'), 'zero outcome count must be visible');
    assert(html.includes('width="0"'), 'zero width must be numeric');
  },
  scale: function() {
    var html = context.statsChart([
      {x: 0, label: 'a', value: 0.02}, {x: 1, label: 'b', value: 0.05}
    ], 'Cost', String);
    assert(html.includes('cy="6"'), 'observed maximum must reach chart top padding');
    assert(html.includes('cy="42"'), 'smaller value must retain its proportion');
    html = context.statsBars([{label: 'a', value: 0.02}, {label: 'b', value: 0.05}], 'Cost');
    assert(html.includes('width="100"'), 'largest bar must span full width');
    assert(html.includes('width="40"'), 'smaller bar must retain its proportion');
    [[], [{x: 0, label: 'zero', value: 0}], [{x: 0, label: 'missing', value: null}]].forEach(function(points) {
      assert(!/NaN|Infinity/.test(context.statsChart(points, 'Empty or zero', String)));
    });
  },
  coordinates: function() {
    var html = context.statsChart([
      {x: -0.5, label: 'a', value: 1}, {x: 0, label: 'b', value: 2}
    ], 'Numeric x', String, [{x: -0.25, label: 'edit'}]);
    assert(html.includes('cx="6"'), 'first numeric x must start at left padding');
    assert(html.includes('cx="314"'), 'last numeric x must reach right padding');
    assert(html.includes('M 160 0 V 72'), 'edit must use the same numeric scale');
  },
  definitions: function() {
    var body = {window: '7d', end: '2026-10-15', definitions: {runs: 'Recorded cohort runs.'}};
    var html = context.personaStatValue(body, {}, 'runs', 'count', 0);
    html += context.personaStatValue(body, {}, 'runs', 'count', 1);
    var ids = [];
    Array.from(html.matchAll(/<span\b([^>]*)data-metric="runs"([^>]*)>/g)).forEach(function(match) {
      var attrs = match[1] + match[2];
      assert(/tabindex="0"/.test(attrs), 'each figure must be focusable');
      var described = attrs.match(/aria-describedby="([^"]+)"/);
      assert(described, 'each figure must have a description');
      assert(!ids.includes(described[1]), 'description ids must be unique across repeated metrics');
      ids.push(described[1]);
      assert(html.includes('id="' + described[1] + '"'), 'description target must exist');
    });
    assert.strictEqual(ids.length, 2);
    assert(html.includes('Recorded cohort runs.'), 'definition must be available as text');
  },
  refresh: function() {
    var mounted;
    var section = {insertAdjacentHTML: function(position, html) { mounted = html; }};
    context.document = {getElementById: function(id) { return id === 'personas-page-section' ? section : {}; }};
    context.agentLabel = String;
    context.personaStatsSelect = function() {};
    context.personaStatsMount([{persona: {role: 'reviewer'}}]);
    var attrs = mounted.match(/<div([^>]*id="persona-stats-content"[^>]*)>/)[1];
    assert(!/aria-live|role="(?:status|alert|log)"/.test(attrs), 'refresh must not announce the whole panel');
  }
};
checks[process.argv[2]]();
console.log(process.argv[2] + ' regression checks passed');
