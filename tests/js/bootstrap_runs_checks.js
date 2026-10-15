/* Bootstrap run markup and guarded actions, executed without sockets. */
var assert = require('assert');
var fs = require('fs');
var vm = require('vm');
var ctx = {
  esc: function(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); },
  timeHtml: function(v) { return String(v); },
  afEventRowHtml: function(e) { return '<article>' + ctx.esc(e.text) + '</article>'; },
  afEntryFromBackend: function(e, run) {
    return {activity_type: e.type, text: e.text, timestamp: e.timestamp, session_id: run};
  },
  currentScopedState: function() { return {symphonies: [{name: 'alpha'}]}; },
  document: {getElementById: function() { return null; }}
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('src/coordinare/dashboard/dashboard_static/bootstrap.js', 'utf8'), ctx);
var run = {run_id: 'r1', symphony: 'alpha', backend: '<backend>', model: 'model', outcome: 'running',
  job_id: 'job', performer_id: 'perf', commit: 'abc', started_at: 'now', duration_seconds: 4,
  last_activity_at: 'then', manifest: [{name: '<package>', version: '1'}],
  verify_verdict: 'failed', reason_tail: ['<secret>'], events: [{text: '<tool>'}], history: {attempt: 1}};
var html = ctx.bootstrapRunHtml(run);
['&lt;backend>', '&lt;package>', '&lt;secret>', '&lt;tool>', 'Retry now', 'Cancel', '/runs/job'].forEach(function(s) {
  assert(html.indexOf(s) !== -1, s);
});
assert(html.indexOf('<package>') === -1);
assert(ctx.bootstrapRunPath('alpha', 'r1') === '/symphonies/alpha/bootstrap/r1');
assert(ctx.isBootstrapRunPath('/symphonies/alpha/bootstrap/r1'));
assert(!ctx.isBootstrapRunPath('/symphonies/alpha'));
assert(ctx.bootstrapActionMessage({error: 'Cooldown', next_reset_at: 'tomorrow'}).indexOf('tomorrow') !== -1);
var first = {type: 'tool_use', text: 'install', timestamp: 'now'};
ctx.bootstrapMergeActivity('r1', [first]);
assert.strictEqual(ctx.bootstrapMergeActivity('r1', [first]).length, 1);
assert.strictEqual(ctx.bootstrapMergeActivity('r1', []).length, 1);
assert.strictEqual(ctx.bootstrapMergeActivity('r2', []).length, 0);
var rowLink;
ctx.afEventRowHtml = function(e) { rowLink = e.session_id; return ''; };
ctx.bootstrapEventRow(first, {run_id: 'stable-run', job_id: 'transcript-job'});
assert.strictEqual(rowLink, 'transcript-job');

function nextTurn() {
  return new Promise(function(resolve) { setImmediate(resolve); });
}

function checkActionsAndScope() {
  var calls = [];
  var confirmed = false;
  ctx.location = {pathname: '/symphonies/alpha/bootstrap/r1'};
  ctx.window = {confirm: function() { return confirmed; }};
  ctx.fetch = function(url, options) {
    calls.push({url: url, options: options});
    return Promise.resolve({json: function() { return Promise.resolve({error: 'Cooldown', next_reset_at: 'tomorrow'}); }});
  };
  var button = {disabled: false};
  var message = {textContent: ''};
  ctx.bootstrapAction('alpha', false, button, message);
  assert.strictEqual(calls.length, 0);
  confirmed = true;
  ctx.bootstrapAction('alpha', false, button, message);
  return nextTurn().then(function() {
    assert.strictEqual(calls[0].url, '/api/symphonies/alpha/env-bootstrap');
    assert.strictEqual(calls[0].options.credentials, 'same-origin');
    assert(message.textContent.indexOf('tomorrow') !== -1);
    assert.strictEqual(button.disabled, false);
    ctx.bootstrapAction('alpha', true, button, message);
    return nextTurn();
  }).then(function() {
    assert.strictEqual(calls[1].url, '/api/symphonies/alpha/env-bootstrap/cancel');
    var detail = {textContent: 'old secret'};
    ctx.document.getElementById = function() { return detail; };
    ctx.currentSymphonyChoice = function() { return 'beta'; };
    var before = calls.length;
    ctx.bootstrapPageRefresh();
    assert.strictEqual(calls.length, before);
    assert.strictEqual(detail.textContent, 'Hidden: another symphony is shown');
    console.log('bootstrap run checks passed');
  });
}
checkActionsAndScope().catch(function(error) { console.error(error); process.exitCode = 1; });
