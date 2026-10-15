/* Exercise shipped helpers and asynchronous request handling without a browser. */
var assert = require('assert');
var fs = require('fs');
var vm = require('vm');
var section = {innerHTML: '', getAttribute: function() { return '1'; }};
var requests = [];
var ctx = {
  console: console, Date: Date, Promise: Promise,
  location: {pathname: '/logs'},
  document: {getElementById: function() { return section; }},
  currentScopedState: function() { return {}; },
  currentSymphonyChoice: function() { return 'alpha'; },
  SYMPHONY_ALL: '__all__',
  fetch: function(url) { return new Promise(function(resolve) { requests.push({url: url, resolve: resolve}); }); }
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
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
row.data.transcript_seq = '7';
assert(ctx.logsRowHtml(row).indexOf('#e7') === -1);
row.symphony = '';
assert(ctx.logsRowHtml(row).indexOf('/runs/run') !== -1);
row.run_id = '';
assert(ctx.logsRowHtml(row).indexOf('/runs/') === -1);
ctx.renderLogsPage({});
assert(requests[0].url.indexOf('symphony=alpha') !== -1);
ctx._logsPage.filters.text = 'new';
ctx.loadLogs();
requests[0].resolve({ok: true, json: function() { return Promise.resolve({events: [row], cursor: 'old'}); }});
requests[1].resolve({ok: true, json: function() { return Promise.resolve({events: [], cursor: null}); }});
setImmediate(function() {
  assert(section.innerHTML.indexOf('bad') === -1, 'stale response must be ignored');
  ctx._logsPage.live = false;
  ctx.logsChanged();
  assert.strictEqual(requests.length, 2);
  ctx._logsPage.live = true;
  ctx.logsChanged();
  assert.strictEqual(requests.length, 3);
  ctx.logsChanged();
  assert.strictEqual(requests.length, 3, 'commit bursts are coalesced');
  requests[2].resolve({ok: true, json: function() { return Promise.resolve({events: [], cursor: null}); }});
  setImmediate(function() {
    assert.strictEqual(requests.length, 4, 'commit during request must trigger a catchup');
    requests[3].resolve({ok: true, json: function() { return Promise.resolve({events: [], cursor: 'older-cursor'}); }});
    setImmediate(function() {
      ctx.logsAction('older');
      assert.strictEqual(ctx._logsPage.live, false);
      assert.strictEqual(ctx._logsPage.index, 1);
      assert(requests[4].url.indexOf('cursor=older-cursor') !== -1);
      ctx.currentSymphonyChoice = function() { return 'beta'; };
      ctx.renderLogsPage({});
      assert(requests[5].url.indexOf('symphony=beta') !== -1);
      assert(requests[5].url.indexOf('cursor=') === -1);
      requests[4].resolve({ok: true, json: function() { return Promise.resolve({events: [row], cursor: 'old'}); }});
      requests[5].resolve({ok: false, status: 503});
      setImmediate(function() {
        assert(section.innerHTML.indexOf('unavailable (503)') !== -1);
        assert(section.innerHTML.indexOf('bad') === -1);
        assert.strictEqual(ctx._logsPage.busy, false);
        ctx.renderLogsPage({}, true);
        assert.strictEqual(requests.length, 7, 'revisiting the page refreshes it');
        console.log('Logs checks passed');
      });
    });
  });
});
