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
console.log('SUMMARY ok');
