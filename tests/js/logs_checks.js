/* Run standalone: node tests/js/logs_checks.js. Load the shipped helpers directly. */
var assert = require('assert');
var fs = require('fs');
var path = require('path');
var vm = require('vm');
var DEBOUNCE_MS = 300;
var PARTIAL_WAIT_MS = 200;
var requests = [];
var timers = {};
var timerId = 0;
var now = 0;
var renderedRows = [];
var content = '';
var section = {
  getAttribute: function() { return '1'; },
  querySelectorAll: function(selector) {
    assert.strictEqual(selector, 'tr[data-event-id]');
    return renderedRows;
  }
};
Object.defineProperty(section, 'innerHTML', {
  get: function() { return content; },
  set: function(value) {
    content = value;
    renderedRows = [];
    var pattern = /<tr data-event-id="([^"]+)">([\s\S]*?)<\/tr>/g;
    var match;
    while ((match = pattern.exec(value))) {
      (function(id, html) {
        var details = {open: html.indexOf('<details open>') !== -1};
        renderedRows.push({
          getAttribute: function(name) { assert.strictEqual(name, 'data-event-id'); return id; },
          querySelector: function(name) { assert.strictEqual(name, 'details'); return details; }
        });
      })(match[1], match[2]);
    }
  }
});
var ctx = {
  console: console, Date: Date, Promise: Promise,
  location: {pathname: '/logs'},
  document: {getElementById: function() { return section; }},
  currentScopedState: function() { return {}; },
  currentSymphonyChoice: function() { return 'alpha'; },
  SYMPHONY_ALL: '__all__',
  setTimeout: function(callback, delay) {
    timerId += 1;
    timers[timerId] = {callback: callback, at: now + delay};
    return timerId;
  },
  clearTimeout: function(id) { delete timers[id]; },
  fetch: function(url) { return new Promise(function(resolve) { requests.push({url: url, resolve: resolve}); }); }
};
function advance(ms) {
  now += ms;
  Object.keys(timers).forEach(function(id) {
    if (timers[id] && timers[id].at <= now) {
      var callback = timers[id].callback;
      delete timers[id];
      callback();
    }
  });
}
function settle() { return new Promise(function(resolve) { setImmediate(resolve); }); }
function respond(index, body) {
  requests[index].resolve({ok: true, json: function() { return Promise.resolve(body); }});
  return settle();
}
vm.createContext(ctx);
var staticDir = path.resolve(__dirname, '../../src/coordinare/dashboard/dashboard_static');
['helpers', 'runs', 'cardpage', 'logs'].forEach(function(name) {
  vm.runInContext(fs.readFileSync(path.join(staticDir, name + '.js'), 'utf8'), ctx);
});
var query = ctx.logsQueryString({symphony: 'alpha', text: 'a&b'}, 'cursor+');
assert(query.indexOf('text=a%26b') !== -1);
assert(query.indexOf('cursor=cursor%2B') !== -1);
var row = {id: 1, ts: '2026-10-01T00:00:00Z', symphony: 'alpha', card_id: 'card', run_id: 'run',
  level: 'info', source: 'performer', kind: 'tool', message: '<script>bad</script>', data: {transcript_seq: 7, text: '<b>'}};
var html = ctx.logsRowHtml(row);
assert(html.indexOf('<script>') === -1);
assert(html.indexOf('&lt;script&gt;') !== -1);
assert(html.indexOf('/cards/alpha/card') !== -1);
assert(html.indexOf('/runs/alpha/card/run#e7') !== -1);
assert(html.indexOf('<details>') !== -1);
assert(html.indexOf('&lt;b&gt;') !== -1);
assert(html.indexOf('<time') !== -1);
['../card', '<script>', '_conductor', 'a b'].forEach(function(card) {
  row.card_id = card;
  html = ctx.logsRowHtml(row);
  assert(html.indexOf('/cards/') === -1, 'invalid address must be plain text');
  assert(html.indexOf(ctx.esc(card)) !== -1);
});
row.card_id = 'card';
row.symphony = 'unattributed';
assert(ctx.logsRowHtml(row).indexOf('/cards/') === -1);
row.symphony = 'alpha';
row.data.transcript_seq = '7';
assert(ctx.logsRowHtml(row).indexOf('#e7') === -1);
row.symphony = '';
assert(ctx.logsRowHtml(row).indexOf('/runs/run') !== -1);
row.run_id = '';
assert(ctx.logsRowHtml(row).indexOf('/runs/') === -1);
ctx.logsPaint({events: [row]});
renderedRows[0].querySelector('details').open = true;
var secondRow = {id: 2, message: 'new row', data: {}};
ctx.logsPaint({events: [secondRow, row]});
assert.strictEqual(renderedRows[0].querySelector('details').open, false, 'new rows start closed');
assert.strictEqual(renderedRows[1].querySelector('details').open, true, 'expansion follows event id after reorder');
renderedRows[1].querySelector('details').open = false;
ctx.logsPaint({events: [row]});
assert.strictEqual(renderedRows[0].querySelector('details').open, false, 'closed panels stay closed');
ctx.renderLogsPage({});
assert(requests[0].url.indexOf('symphony=alpha') !== -1);
ctx._logsPage.filters.text = 'new';
ctx.loadLogs();
respond(0, {events: [row], cursor: 'old'}).then(function() {
  return respond(1, {events: [], cursor: null});
}).then(function() {
  assert(section.innerHTML.indexOf('bad') === -1, 'stale response must be ignored');
  ctx._logsPage.live = false;
  ctx.logsChanged();
  advance(DEBOUNCE_MS);
  assert.strictEqual(requests.length, 2);
  ctx._logsPage.live = true;
  ctx.logsChanged();
  assert.strictEqual(requests.length, 2, 'live changes wait for the trailing debounce');
  advance(PARTIAL_WAIT_MS);
  ctx.logsChanged();
  advance(PARTIAL_WAIT_MS);
  assert.strictEqual(requests.length, 2, 'a later change restarts the debounce');
  advance(DEBOUNCE_MS - PARTIAL_WAIT_MS);
  assert.strictEqual(requests.length, 3);
  ctx.logsChanged();
  advance(DEBOUNCE_MS);
  assert.strictEqual(requests.length, 3, 'no overlapping live request');
  return respond(2, {events: [], cursor: null});
}).then(function() {
  assert.strictEqual(requests.length, 3, 'catchup also observes the debounce');
  advance(DEBOUNCE_MS);
  assert.strictEqual(requests.length, 4, 'a change during a request triggers a catchup');
  return respond(3, {events: [], cursor: 'older-cursor'});
}).then(function() {
  ctx.logsChanged();
  ctx.logsAction('older');
  assert.strictEqual(ctx._logsPage.live, false);
  assert.strictEqual(ctx._logsPage.index, 1);
  assert(requests[4].url.indexOf('cursor=older-cursor') !== -1);
  advance(DEBOUNCE_MS);
  assert.strictEqual(requests.length, 5, 'pausing cancels a scheduled live refresh');
  ctx.currentSymphonyChoice = function() { return 'beta'; };
  ctx.renderLogsPage({});
  assert(requests[5].url.indexOf('symphony=beta') !== -1);
  assert(requests[5].url.indexOf('cursor=') === -1);
  requests[4].resolve({ok: true, json: function() { return Promise.resolve({events: [row], cursor: 'old'}); }});
  requests[5].resolve({ok: false, status: 503});
  return settle();
}).then(function() {
  assert(section.innerHTML.indexOf('unavailable (503)') !== -1);
  assert(section.innerHTML.indexOf('bad') === -1);
  assert.strictEqual(ctx._logsPage.busy, false);
  ctx._logsPage.live = true;
  ctx.logsChanged();
  ctx.location.pathname = '/';
  advance(DEBOUNCE_MS);
  assert.strictEqual(requests.length, 6, 'leaving the page cancels live work');
  ctx.location.pathname = '/logs';
  ctx.renderLogsPage({}, true);
  assert.strictEqual(requests.length, 7, 'revisiting the page refreshes it');
  requests[6].resolve({ok: false, status: 422, json: function() {
    return Promise.resolve({error: 'query too broad, narrow the filters'});
  }});
  return settle();
}).then(function() {
  assert(section.innerHTML.indexOf('query too broad, narrow the filters') !== -1,
    'an interrupted query tells the operator to narrow the filters');
  console.log('Logs checks passed');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});
