const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const response = {
  window: '30d', start: '2026-09-16T12:00:00Z', end: '2026-10-16T12:00:00Z',
  partial: false, definitions: {},
  totals: {runs: 1, tokens: {total: 100}, cost: {usd: 1}, missing_usage_runs: 0},
  reliability: {alpha: {checks: 2, success_rate: 0.5, recent_errors: []}}
};
const complete = symphonyStatsHtml('alpha', response, response);
assert.ok(complete.includes('50%</strong>'));
assert.ok(!complete.includes('Board check history is partial'));
['event_store.retention_days: 7 days', 'event_store.max_bytes removed hourly totals <inside>'].forEach(reason => {
  response.reliability.alpha.partial = true;
  response.reliability.alpha.partial_reasons = [reason];
  const html = symphonyStatsHtml('alpha', response, response);
  assert.ok(html.includes('50% (partial)</strong>'));
  assert.ok(html.includes('2 (partial)</strong>'));
  assert.ok(html.includes('Board check history is partial: ' + esc(reason)));
  assert.ok(!html.includes('<inside>'));
  assert.ok(html.includes('$1.00'), 'run spend remains complete');
  assert.ok(!/\bsessions?\b|\battempts?\b/i.test(html));
  assert.ok(!/[\u2013\u2014]/.test(html));
});
console.log('309 reliability partial rendering passed');
