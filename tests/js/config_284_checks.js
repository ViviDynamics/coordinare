'use strict';
var assert = require('assert');
function esc(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
assert.strictEqual(cfgSeedBanner(null), '');
var banner = cfgSeedBanner({chart_changed: true, message: 'chart values changed since this config was seeded',
  changed_sections: ['<img src=x>'], live_diff_sections: ['github_org'], reset_instruction: 'Back up then reset <carefully>'});
assert.ok(banner.indexOf('chart values changed since this config was seeded') >= 0);
assert.ok(banner.indexOf('&lt;img src=x&gt;') >= 0);
assert.ok(banner.indexOf('<img src=x>') < 0);
assert.ok(banner.indexOf('&lt;carefully&gt;') >= 0);
assert.ok(banner.indexOf('github_org') >= 0);
assert.ok(cfgSeedBanner({chart_changed: null, message: 'Config seed provenance is unknown'}).indexOf('unknown') >= 0);
assert.ok(cfgSeedBanner({chart_changed: true, message: 'changed', changed_sections: []}).indexOf('formatting') >= 0);
assert.ok(banner.indexOf('Adopt chart values') >= 0);
var diff = cfgChartDiffHtml({sections: [{name: '<img src=x>', live: {token: 'masked'}, chart: '<script>new</script>'}]});
assert.ok(diff.indexOf('&lt;img src=x&gt;') >= 0);
assert.ok(diff.indexOf('&lt;script&gt;new&lt;/script&gt;') >= 0);
assert.ok(diff.indexOf('<script>') < 0);
assert.ok(diff.indexOf('Confirm adoption') >= 0);
assert.ok(diff.indexOf('data-chart-section') >= 0);
var chartStatus = {textContent: ''};
var inputs = [{disabled: false, getAttribute: function() { return 'github_org'; }}];
var box = {
  textContent: '', innerHTML: '',
  querySelector: function() { return chartStatus; },
  querySelectorAll: function(selector) {
    return selector === '[data-chart-section]:checked' ? inputs : inputs;
  }
};
var document = {getElementById: function() { return box; }};
var requests = [];
function fetch(url, options) {
  requests.push({url: url, options: options});
  return Promise.resolve({ok: true, headers: {get: function() { return '"live-hash"'; }},
    json: function() { return Promise.resolve(options ? {applied: 'hot_reloaded'} : {
      seed_hash: 'chart-hash', sections: [{name: 'github_org', live: 'old', chart: 'new'}]
    }); }
  });
}
cfgReviewChart().then(function() {
  assert.strictEqual(requests.length, 1);
  assert.strictEqual(requests[0].options, undefined);
  assert.ok(box.innerHTML.indexOf('Confirm adoption') >= 0);
  assert.strictEqual(_cfgChartPreview.hash, '"live-hash"');
  return cfgConfirmChart();
}).then(function() {
  assert.strictEqual(requests.length, 2);
  assert.strictEqual(requests[1].url, '/api/config/chart/adopt');
  assert.strictEqual(requests[1].options.method, 'POST');
  assert.strictEqual(requests[1].options.headers['If-Match'], '"live-hash"');
  assert.deepStrictEqual(JSON.parse(requests[1].options.body), {
    sections: ['github_org'], seed_hash: 'chart-hash', confirmed: true
  });
  assert.strictEqual(chartStatus.textContent, 'Chart sections saved.');
  assert.strictEqual(_cfgChartPreview, null);
  cfgConfirmChart();
  assert.strictEqual(requests.length, 2);
  console.log('SUMMARY ok');
}).catch(function(error) { console.error(error); process.exitCode = 1; });
