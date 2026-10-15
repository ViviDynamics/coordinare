'use strict';
var fs = require('fs');
var vm = require('vm');
var assert = require('assert');
var path = require('path');
var root = path.resolve(__dirname, '../../src/coordinare/dashboard/dashboard_static');
var context = {
  esc: function(value) { return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
  document: {getElementById: function() { return null; }},
  location: {search: '', pathname: '/'}, window: {localStorage: null}, console: console
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root, 'layout.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(root, 'symphonies.js'), 'utf8'), context);
var html = context.activeRunsIdleHtml({idle_reason: '<img src=x onerror=alert(1)>', ownership_hint: '<script>'});
assert(html.indexOf('<img') === -1);
assert(html.indexOf('&lt;img') !== -1);
assert(html.indexOf('<script>') === -1);
assert(html.indexOf('FIRST_CARD.md') !== -1);
assert(html.indexOf('Try a first card') !== -1);
assert.strictEqual(context.SNAPSHOT_FIELD_SCOPE.idle_reason, 'derived');
var state = {
  idle_reason: 'alpha reason', active_sessions: [],
  symphonies: [{name: 'alpha', idle_reason: 'alpha reason', state: {}},
               {name: 'beta', idle_reason: 'beta reason', state: {}}]
};
assert.strictEqual(context.symphonyScope(state, 'beta').idle_reason, 'beta reason');
console.log('First launch client checks passed');
